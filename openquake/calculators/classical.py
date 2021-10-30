# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2014-2021 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.

import io
import psutil
import logging
import operator
import collections
import numpy
try:
    from PIL import Image
except ImportError:
    Image = None
from openquake.baselib import parallel, hdf5, config
from openquake.baselib.python3compat import encode
from openquake.baselib.general import (
    AccumDict, DictArray, block_splitter, groupby, humansize,
    get_nbytes_msg)
from openquake.hazardlib.contexts import ContextMaker, read_cmakers
from openquake.hazardlib.calc.hazard_curve import classical as hazclassical
from openquake.hazardlib.probability_map import ProbabilityMap
from openquake.commonlib import calc, readinput
from openquake.calculators import getters
from openquake.calculators import base, preclassical

U16 = numpy.uint16
U32 = numpy.uint32
F32 = numpy.float32
F64 = numpy.float64
TWO32 = 2 ** 32
BUFFER = 1.5  # enlarge the pointsource_distance sphere to fix the weight;
# with BUFFER = 1 we would have lots of apparently light sources
# collected together in an extra-slow task, as it happens in SHARE
# with ps_grid_spacing=50
get_weight = operator.attrgetter('weight')
disagg_grp_dt = numpy.dtype([
    ('grp_start', U16), ('grp_trt', hdf5.vstr), ('avg_poe', F32),
    ('smrs', hdf5.vuint16)])


def get_source_id(src):  # used in submit_tasks
    return src.source_id.split(':')[0]


def store_ctxs(dstore, rupdata, grp_id):
    """
    Store contexts with the same magnitude in the datastore
    """
    nr = len(rupdata['mag'])
    rupdata['grp_id'] = numpy.repeat(grp_id, nr)
    nans = numpy.repeat(numpy.nan, nr)
    for par in dstore['rup']:
        n = 'rup/' + par
        if par.endswith('_'):
            if par in rupdata:
                dstore.hdf5.save_vlen(n, rupdata[par])
            else:  # add nr empty rows
                dstore[n].resize((len(dstore[n]) + nr,))
        else:
            hdf5.extend(dstore[n], rupdata.get(par, nans))


#  ########################### task functions ############################ #


def classical(srcs, cmaker, monitor):
    """
    Read the sitecol and call the classical calculator in hazardlib
    """
    cmaker.init_monitoring(monitor)
    return hazclassical(srcs, monitor.read('sitecol'), cmaker)


class Hazard:
    """
    Helper class for storing the PoEs
    """
    def __init__(self, dstore, full_lt, pgetter, srcidx, mon):
        self.datastore = dstore
        self.full_lt = full_lt
        self.cmakers = read_cmakers(dstore, full_lt)
        self.get_hcurves = pgetter.get_hcurves
        self.imtls = imtls = pgetter.imtls
        self.level_weights = imtls.array / imtls.array.sum()
        self.sids = pgetter.sids
        self.srcidx = srcidx
        self.mon = mon
        self.N = len(dstore['sitecol/sids'])
        self.acc = AccumDict(accum={})

    def init(self, pmaps, grp_id):
        """
        Initialize the pmaps dictionary with zeros, if needed
        """
        L, G = self.imtls.size, len(self.cmakers[grp_id].gsims)
        pmaps[grp_id] = ProbabilityMap.build(L, G, self.sids)

    def store_poes(self, grp_id, pmap):
        """
        Store the pmap of the given group inside the _poes dataset
        """
        G = pmap.shape_z
        with self.mon:
            cmaker = self.cmakers[grp_id]
            dset = self.datastore['_poes']
            values = []
            for sid in pmap:
                arr = pmap[sid].array.T  # shape (G, L)
                if arr.any():  # nonzero
                    dset[sid, cmaker.start:cmaker.start + G] = arr
                values.append(arr @ self.level_weights)
            self.acc[grp_id]['grp_start'] = cmaker.start
            self.acc[grp_id]['avg_poe'] = numpy.mean(values)

    def store_disagg(self, pmaps=None):
        """
        Store data inside disagg_by_grp (optionally disagg_by_src)
        """
        n = len(self.full_lt.sm_rlzs)
        lst = []
        for grp_id, indices in enumerate(self.datastore['trt_smrs']):
            dic = self.acc[grp_id]
            if dic:
                trti, smrs = numpy.divmod(indices, n)
                trt = self.full_lt.trts[trti[0]]
                lst.append((dic['grp_start'], trt, dic['avg_poe'], smrs))
        self.datastore['disagg_by_grp'] = numpy.array(lst, disagg_grp_dt)

        if pmaps:  # called inside a loop
            for key, pmap in pmaps.items():
                if isinstance(key, str):
                    # contains only string keys in case of disaggregation
                    rlzs_by_gsim = self.cmakers[pmap.grp_id].gsims
                    self.datastore['disagg_by_src'][..., self.srcidx[key]] = (
                        self.get_hcurves(pmap, rlzs_by_gsim))


@base.calculators.add('classical', 'ucerf_classical')
class ClassicalCalculator(base.HazardCalculator):
    """
    Classical PSHA calculator
    """
    core_task = classical
    precalc = 'preclassical'
    accept_precalc = ['preclassical', 'classical']

    def agg_dicts(self, acc, dic):
        """
        Aggregate dictionaries of hazard curves by updating the accumulator.

        :param acc: accumulator dictionary
        :param dic: dict with keys pmap, calc_times, rup_data
        """
        # NB: dic should be a dictionary, but when the calculation dies
        # for an OOM it can become None, thus giving a very confusing error
        if dic is None:
            raise MemoryError('You ran out of memory!')

        ctimes = dic['calc_times']  # srcid -> eff_rups, eff_sites, dt
        self.calc_times += ctimes
        srcids = set()
        eff_rups = 0
        eff_sites = 0
        for srcid, rec in ctimes.items():
            srcids.add(srcid)
            eff_rups += rec[0]
            if rec[0]:
                eff_sites += rec[1] / rec[0]
        self.by_task[dic['task_no']] = (
            eff_rups, eff_sites, sorted(srcids))
        grp_id = dic.pop('grp_id')
        self.rel_ruptures[grp_id] += eff_rups

        # store rup_data if there are few sites
        if self.few_sites and len(dic['rup_data']['src_id']):
            with self.monitor('saving rup_data'):
                store_ctxs(self.datastore, dic['rup_data'], grp_id)

        pmap = dic['pmap']
        pmap.grp_id = grp_id
        source_id = dic.pop('source_id', None)
        if source_id:
            # store the poes for the given source
            acc[source_id.split(':')[0]] = pmap
        if pmap:
            if self.counts[grp_id] == 1:  # shortcut to save memory
                self.haz.store_poes(grp_id, pmap)
            else:
                acc[grp_id] |= pmap
        return acc

    def create_dsets(self):
        """
        Store some empty datasets in the datastore
        """
        self.init_poes()
        params = {'grp_id', 'occurrence_rate', 'clon_', 'clat_', 'rrup_',
                  'probs_occur_', 'sids_', 'src_id'}
        gsims_by_trt = self.full_lt.get_gsims_by_trt()
        for trt, gsims in gsims_by_trt.items():
            cm = ContextMaker(trt, gsims, dict(imtls=self.oqparam.imtls))
            params.update(cm.REQUIRES_RUPTURE_PARAMETERS)
            for dparam in cm.REQUIRES_DISTANCES:
                params.add(dparam + '_')
        mags = set()
        for trt, dset in self.datastore['source_mags'].items():
            mags.update(dset[:])
        mags = sorted(mags)
        if self.few_sites:
            descr = []  # (param, dt)
            for param in params:
                if param == 'sids_':
                    dt = hdf5.vuint16
                elif param == 'probs_occur_':
                    dt = hdf5.vfloat64
                elif param.endswith('_'):
                    dt = hdf5.vfloat32
                elif param == 'src_id':
                    dt = U32
                elif param == 'grp_id':
                    dt = U16
                else:
                    dt = F32
                descr.append((param, dt))
            self.datastore.create_df('rup', descr, 'gzip')
        self.by_task = {}  # task_no => src_ids
        self.Ns = len(self.csm.source_info)
        self.rel_ruptures = AccumDict(accum=0)  # grp_id -> rel_ruptures
        # NB: the relevant ruptures are less than the effective ruptures,
        # which are a preclassical concept
        if self.oqparam.disagg_by_src:
            sources = self.get_source_ids()
            self.datastore.create_dset(
                'disagg_by_src', F32,
                (self.N, self.R, self.M, self.L1, self.Ns))
            self.datastore.set_shape_descr(
                'disagg_by_src', site_id=self.N, rlz_id=self.R,
                imt=list(self.oqparam.imtls), lvl=self.L1, src_id=sources)

    def get_source_ids(self):
        """
        :returns: the unique source IDs contained in the composite model
        """
        oq = self.oqparam
        self.M = len(oq.imtls)
        self.L1 = oq.imtls.size // self.M
        sources = encode([src_id for src_id in self.csm.source_info])
        size, msg = get_nbytes_msg(
            dict(N=self.N, R=self.R, M=self.M, L1=self.L1, Ns=self.Ns))
        ps = 'pointSource' in self.full_lt.source_model_lt.source_types
        if size > TWO32 and not ps:
            raise RuntimeError('The matrix disagg_by_src is too large: %s'
                               % msg)
        elif size > TWO32:
            msg = ('The source model contains point sources: you cannot set '
                   'disagg_by_src=true unless you convert them to multipoint '
                   'sources with the command oq upgrade_nrml --multipoint %s'
                   ) % oq.base_path
            raise RuntimeError(msg)
        return sources

    def init_poes(self):
        if self.oqparam.hazard_calculation_id:
            full_lt = self.datastore.parent['full_lt']
            trt_smrs = self.datastore.parent['trt_smrs'][:]
        else:
            full_lt = self.csm.full_lt
            trt_smrs = self.csm.get_trt_smrs()
        self.grp_ids = numpy.arange(len(trt_smrs))
        rlzs_by_gsim_list = full_lt.get_rlzs_by_gsim_list(trt_smrs)
        rlzs_by_g = []
        for rlzs_by_gsim in rlzs_by_gsim_list:
            for rlzs in rlzs_by_gsim.values():
                rlzs_by_g.append(rlzs)

        poes_shape = self.N, len(rlzs_by_g)
        self.ct = self.oqparam.concurrent_tasks * 1.5 or 1
        # NB: it is CRITICAL for performance to access the first dimension
        self.datastore.create_dset(
            '_poes', hdf5.vfloat64, poes_shape, fillvalue=None)
        if not self.oqparam.hazard_calculation_id:
            self.datastore.swmr_on()

    def check_memory(self, N, L, num_gs):
        G = sum(num_gs)
        size = G * N * L * 8
        bytes_per_grp = size / len(self.grp_ids)
        avail = min(psutil.virtual_memory().available, config.memory.limit)
        logging.info('Requiring %s for full ProbabilityMap of shape %s',
                     humansize(size), (G, N, L))
        maxsize = 20_000 * max(num_gs) * N * self.oqparam.imtls.size * 8
        logging.info('Requiring at max %s for gen_poes', humansize(maxsize))
        if avail < bytes_per_grp:
            raise MemoryError(
                'You have only %s of free RAM' % humansize(avail))
        elif avail < size:
            logging.warning('You have only %s of free RAM' % humansize(avail))

    def execute(self):
        """
        Run in parallel `core_task(sources, sitecol, monitor)`, by
        parallelizing on the sources according to their weight and
        tectonic region type.
        """
        oq = self.oqparam
        psd = preclassical.PreClassicalCalculator.set_psd(self)
        if oq.hazard_calculation_id:
            parent = self.datastore.parent
            if '_poes' in parent:
                self.post_classical()  # repeat post-processing
                return {}
            else:  # after preclassical, like in case_36
                self.csm = parent['_csm']
                self.full_lt = parent['full_lt']
                num_srcs = len(self.csm.source_info)
                self.datastore.hdf5.create_dataset(
                    'source_info', (num_srcs,), readinput.source_info_dt)
        self.create_dsets()  # create the rup/ datasets BEFORE swmr_on()
        grp_ids = numpy.arange(len(self.csm.src_groups))
        self.calc_times = AccumDict(accum=numpy.zeros(3, F32))
        weights = [rlz.weight for rlz in self.realizations]
        pgetter = getters.PmapGetter(
            self.datastore, weights, self.sitecol.sids, oq.imtls)
        srcidx = {
            rec[0]: i for i, rec in enumerate(self.csm.source_info.values())}
        self.haz = Hazard(self.datastore, self.full_lt, pgetter, srcidx,
                          self.monitor('storing _poes', measuremem=True))
        args = self.get_args(grp_ids, self.haz.cmakers)
        self.counts = collections.Counter(arg[0][0].grp_id for arg in args)
        logging.info('grp_id->ntasks: %s', list(self.counts.values()))
        # only groups generating more than 1 task preallocate memory
        num_gs = [len(cm.gsims) for grp, cm in enumerate(self.haz.cmakers)
                  if self.counts[grp] > 1]
        if num_gs:
            self.check_memory(self.N, oq.imtls.size, num_gs)
        h5 = self.datastore.hdf5
        smap = parallel.Starmap(classical, args, h5=h5)
        smap.monitor.save('sitecol', self.sitecol)
        self.datastore.swmr_on()
        smap.h5 = self.datastore.hdf5
        acc = {}
        for grp_id, num_tasks in self.counts.items():
            if num_tasks > 1:
                self.haz.init(acc, grp_id)
        logging.info('Sending %d tasks', len(args))
        smap.reduce(self.agg_dicts, acc)
        logging.debug("busy time: %s", smap.busytime)
        self.store_info(psd)
        logging.info('Saving _poes')
        for grp_id in list(acc):
            if isinstance(grp_id, int):
                self.haz.store_poes(grp_id, acc.pop(grp_id))
        self.haz.store_disagg(acc)
        return True

    def store_info(self, psd):
        """
        Store full_lt, source_info and by_task
        """
        self.store_rlz_info(self.rel_ruptures)
        source_ids = self.store_source_info(self.calc_times)
        if self.by_task:
            logging.info('Storing by_task information')
            num_tasks = max(self.by_task) + 1,
            er = self.datastore.create_dset('by_task/eff_ruptures',
                                            U32, num_tasks)
            es = self.datastore.create_dset('by_task/eff_sites',
                                            U32, num_tasks)
            si = self.datastore.create_dset('by_task/srcids',
                                            hdf5.vstr, num_tasks,
                                            fillvalue=None)
            for task_no, rec in self.by_task.items():
                effrups, effsites, srcids = rec
                er[task_no] = effrups
                es[task_no] = effsites
                si[task_no] = ' '.join(source_ids[s] for s in srcids)
            self.by_task.clear()
        if self.calc_times:  # can be empty in case of errors
            self.numctxs = sum(arr[0] for arr in self.calc_times.values())
            numsites = sum(arr[1] for arr in self.calc_times.values())
            logging.info('Total number of contexts: {:_d}'.
                         format(int(self.numctxs)))
            if self.numctxs:
                logging.info('Average number of sites per context: %d',
                             numsites / self.numctxs)
        self.calc_times.clear()  # save a bit of memory

    def get_args(self, grp_ids, cmakers):
        """
        :returns: a list of Starmap arguments
        """
        oq = self.oqparam
        allargs = []
        src_groups = self.csm.src_groups
        tot_weight = 0
        for grp_id in grp_ids:
            cmaker = cmakers[grp_id]
            gsims = cmaker.gsims
            sg = src_groups[grp_id]
            for src in sg:
                src.ngsims = len(gsims)
                tot_weight += src.weight
                if src.code == b'C' and src.num_ruptures > 20_000:
                    msg = ('{} is suspiciously large, containing {:_d} '
                           'ruptures with complex_fault_mesh_spacing={} km')
                    spc = oq.complex_fault_mesh_spacing
                    logging.info(msg.format(src, src.num_ruptures, spc))
        assert tot_weight
        ct = oq.concurrent_tasks or 1
        max_weight = max(tot_weight / ct, oq.min_weight)
        self.param['max_weight'] = max_weight
        logging.info('tot_weight={:_d}, max_weight={:_d}'.format(
            int(tot_weight), int(max_weight)))
        for grp_id in grp_ids:
            sg = src_groups[grp_id]
            if sg.atomic:
                # do not split atomic groups
                allargs.append((sg, cmakers[grp_id]))
            else:  # regroup the sources in blocks
                blks = (groupby(sg, get_source_id).values()
                        if oq.disagg_by_src else
                        block_splitter(sg, max_weight, get_weight, sort=True))
                blocks = list(blks)
                for block in blocks:
                    logging.debug('Sending %d source(s) with weight %d',
                                  len(block), sum(src.weight for src in block))
                    allargs.append((block, cmakers[grp_id]))
        return allargs

    def save_hazard(self, acc, pmap_by_kind):
        """
        Works by side effect by saving hcurves and hmaps on the datastore

        :param acc: ignored
        :param pmap_by_kind: a dictionary of ProbabilityMaps
        """
        with self.monitor('collecting hazard'):
            if pmap_by_kind is None:  # instead of a dict
                raise MemoryError('You ran out of memory!')
            for kind in pmap_by_kind:  # hmaps-XXX, hcurves-XXX
                pmaps = pmap_by_kind[kind]
                if kind in self.hazard:
                    array = self.hazard[kind]
                else:
                    dset = self.datastore.getitem(kind)
                    array = self.hazard[kind] = numpy.zeros(
                        dset.shape, dset.dtype)
                for r, pmap in enumerate(pmaps):
                    for s in pmap:
                        if kind.startswith('hmaps'):
                            array[s, r] = pmap[s].array  # shape (M, P)
                        else:  # hcurves
                            array[s, r] = pmap[s].array.reshape(-1, self.L1)

    def post_execute(self, dummy):
        """
        Compute the statistical hazard curves
        """
        task_info = self.datastore.read_df('task_info', 'taskname')
        try:
            dur = task_info.loc[b'classical'].duration
        except KeyError:  # no data
            pass
        else:
            slow_tasks = len(dur[dur > 3 * dur.mean()])
            if slow_tasks:
                logging.info('There were %d slow tasks', slow_tasks)
        nr = {name: len(dset['mag']) for name, dset in self.datastore.items()
              if name.startswith('rup_')}
        if nr:  # few sites, log the number of ruptures per magnitude
            logging.info('%s', nr)
        if (self.oqparam.hazard_calculation_id is None
                and '_poes' in self.datastore):
            self.datastore.swmr_on()  # needed
            self.post_classical()

    def post_classical(self):
        """
        Store hcurves-rlzs, hcurves-stats, hmaps-rlzs, hmaps-stats
        """
        oq = self.oqparam
        if not oq.hazard_curves:  # do nothing
            return
        N = len(self.sitecol)
        R = len(self.realizations)
        if oq.individual_rlzs is None:  # not specified in the job.ini
            individual_rlzs = (N == 1) * (R > 1)
        else:
            individual_rlzs = oq.individual_rlzs
        hstats = oq.hazard_stats()
        # initialize datasets
        P = len(oq.poes)
        M = self.M = len(oq.imtls)
        imts = list(oq.imtls)
        if oq.soil_intensities is not None:
            L = M * len(oq.soil_intensities)
        else:
            L = oq.imtls.size
        L1 = self.L1 = L // M
        S = len(hstats)
        if R > 1 and individual_rlzs or not hstats:
            self.datastore.create_dset('hcurves-rlzs', F32, (N, R, M, L1))
            self.datastore.set_shape_descr(
                'hcurves-rlzs', site_id=N, rlz_id=R, imt=imts, lvl=L1)
            if oq.poes:
                self.datastore.create_dset('hmaps-rlzs', F32, (N, R, M, P))
                self.datastore.set_shape_descr(
                    'hmaps-rlzs', site_id=N, rlz_id=R,
                    imt=list(oq.imtls), poe=oq.poes)
        if hstats:
            self.datastore.create_dset('hcurves-stats', F32, (N, S, M, L1))
            self.datastore.set_shape_descr(
                'hcurves-stats', site_id=N, stat=list(hstats),
                imt=imts, lvl=numpy.arange(L1))
            if oq.poes:
                self.datastore.create_dset('hmaps-stats', F32, (N, S, M, P))
                self.datastore.set_shape_descr(
                    'hmaps-stats', site_id=N, stat=list(hstats),
                    imt=list(oq.imtls), poe=oq.poes)
        ct = oq.concurrent_tasks or 1
        logging.info('Building hazard statistics')
        self.weights = [rlz.weight for rlz in self.realizations]
        dstore = (self.datastore.parent if oq.hazard_calculation_id
                  else self.datastore)
        allargs = [  # this list is very fast to generate
            (getters.PmapGetter(
                dstore, self.weights, t.sids, oq.imtls, oq.poes),
             N, hstats, individual_rlzs, oq.max_sites_disagg,
             self.amplifier)
            for t in self.sitecol.split_in_tiles(ct)]
        if self.few_sites:
            dist = 'no'
        else:
            dist = None  # parallelize as usual
        if oq.hazard_calculation_id is None:  # essential before Starmap
            self.datastore.swmr_on()
        self.hazard = {}  # kind -> array
        parallel.Starmap(
            postclassical, allargs, distribute=dist, h5=self.datastore.hdf5
        ).reduce(self.save_hazard)
        for kind in sorted(self.hazard):
            logging.info('Saving %s', kind)
            self.datastore[kind][:] = self.hazard.pop(kind)
        if 'hmaps-stats' in self.datastore:
            hmaps = self.datastore.sel('hmaps-stats', stat='mean')  # NSMP
            maxhaz = hmaps.max(axis=(0, 1, 3))
            mh = dict(zip(self.oqparam.imtls, maxhaz))
            logging.info('The maximum hazard map values are %s', mh)
            if Image is None or not self.from_engine:  # missing PIL
                return
            M, P = hmaps.shape[2:]
            logging.info('Saving %dx%d mean hazard maps', M, P)
            inv_time = oq.investigation_time
            allargs = []
            for m, imt in enumerate(self.oqparam.imtls):
                for p, poe in enumerate(self.oqparam.poes):
                    dic = dict(m=m, p=p, imt=imt, poe=poe, inv_time=inv_time,
                               calc_id=self.datastore.calc_id,
                               array=hmaps[:, 0, m, p])
                    allargs.append((dic, self.sitecol.lons, self.sitecol.lats))
            smap = parallel.Starmap(make_hmap_png, allargs)
            for dic in smap:
                self.datastore['png/hmap_%(m)d_%(p)d' % dic] = dic['img']


def make_hmap_png(hmap, lons, lats):
    """
    :param hmap:
        a dictionary with keys calc_id, m, p, imt, poe, inv_time, array
    :param lons: an array of longitudes
    :param lats: an array of latitudes
    :returns: an Image object containing the hazard map
    """
    import matplotlib.pyplot as plt
    fig = plt.figure()
    ax = fig.add_subplot(111)
    ax.grid(True)
    ax.set_title('hmap for IMT=%(imt)s, poe=%(poe)s\ncalculation %(calc_id)d,'
                 'inv_time=%(inv_time)dy' % hmap)
    ax.set_ylabel('Longitude')
    coll = ax.scatter(lons, lats, c=hmap['array'], cmap='jet')
    plt.colorbar(coll)
    bio = io.BytesIO()
    plt.savefig(bio, format='png')
    return dict(img=Image.open(bio), m=hmap['m'], p=hmap['p'])


def postclassical(pgetter, N, hstats, individual_rlzs,
                  max_sites_disagg, amplifier, monitor):
    """
    :param pgetter: an :class:`openquake.commonlib.getters.PmapGetter`
    :param N: the total number of sites
    :param hstats: a list of pairs (statname, statfunc)
    :param individual_rlzs: if True, also build the individual curves
    :param max_sites_disagg: if there are less sites than this, store rup info
    :param amplifier: instance of Amplifier or None
    :param monitor: instance of Monitor
    :returns: a dictionary kind -> ProbabilityMap

    The "kind" is a string of the form 'rlz-XXX' or 'mean' of 'quantile-XXX'
    used to specify the kind of output.
    """
    with monitor('read PoEs'):
        pgetter.init()
        if amplifier:
            with hdf5.File(pgetter.filename, 'r') as f:
                ampcode = f['sitecol'].ampcode
            imtls = DictArray({imt: amplifier.amplevels
                               for imt in pgetter.imtls})
        else:
            imtls = pgetter.imtls
    poes, weights = pgetter.poes, pgetter.weights
    M = len(imtls)
    P = len(poes)
    L = imtls.size
    R = len(weights)
    S = len(hstats)
    pmap_by_kind = {}
    if R > 1 and individual_rlzs or not hstats:
        pmap_by_kind['hcurves-rlzs'] = [ProbabilityMap(L) for r in range(R)]
        if poes:
            pmap_by_kind['hmaps-rlzs'] = [
                ProbabilityMap(M, P) for r in range(R)]
    if hstats:
        pmap_by_kind['hcurves-stats'] = [ProbabilityMap(L) for r in range(S)]
        if poes:
            pmap_by_kind['hmaps-stats'] = [
                ProbabilityMap(M, P) for r in range(S)]
    combine_mon = monitor('combine pmaps', measuremem=False)
    compute_mon = monitor('compute stats', measuremem=False)
    for sid in pgetter.sids:
        with combine_mon:
            pc = pgetter.get_pcurve(sid)  # shape (L, R)
            if amplifier:
                pc = amplifier.amplify(ampcode[sid], pc)
                # NB: the pcurve have soil levels != IMT levels
        if pc.array.sum() == 0:  # no data
            continue
        with compute_mon:
            if hstats:
                for s, (statname, stat) in enumerate(hstats.items()):
                    sc = getters.build_stat_curve(pc, imtls, stat, weights)
                    pmap_by_kind['hcurves-stats'][s][sid] = sc
                    if poes:
                        hmap = calc.make_hmap(sc, imtls, poes, sid)
                        pmap_by_kind['hmaps-stats'][s].update(hmap)
            if R > 1 and individual_rlzs or not hstats:
                for r, pmap in enumerate(pmap_by_kind['hcurves-rlzs']):
                    pmap[sid] = pc.extract(r)
                if poes:
                    for r in range(R):
                        hmap = calc.make_hmap(pc.extract(r), imtls, poes, sid)
                        pmap_by_kind['hmaps-rlzs'][r].update(hmap)
    return pmap_by_kind
