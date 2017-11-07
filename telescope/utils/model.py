# -*- coding: utf-8 -*-
from __future__ import print_function
from __future__ import absolute_import

import sys
import os
import logging as lg
from collections import OrderedDict, defaultdict, Counter
import gc

import numpy as np
import scipy
import pysam

from .sparse_plus import csr_matrix_plus as csr_matrix
from .colors import c2str, D2PAL, GPAL
from .helpers import str2int
from .alignment import fetch_fragments

__author__ = 'Matthew L. Bendall'
__copyright__ = "Copyright (C) 2017 Matthew L. Bendall"


def process_overlap_frag(pairs, overlap_feats):
    ''' Find the best alignment for each locus '''
    assert all(pairs[0].query_id == p.query_id for p in pairs)
    ''' Organize by feature'''
    byfeature = defaultdict(list)
    for pair, feat in zip(pairs, overlap_feats):
        byfeature[feat].append(pair)

    _maps = []
    for feat, falns in byfeature.items():
        # Sort alignments by score + length
        falns.sort(key=lambda x: x.alnscore + x.alnlen,
                  reverse=True)
        # Add best alignment to mappings
        _topaln = falns[0]
        _maps.append(
            (_topaln.query_id, feat, _topaln.alnscore, _topaln.alnlen)
        )
        # Set tag for feature (ZF) and whether it is best (ZT)
        _topaln.set_tag('ZF', feat)
        _topaln.set_tag('ZT', 'PRI')
        for aln in falns[1:]:
            aln.set_tag('ZF', feat)
            aln.set_tag('ZT', 'SEC')

    # Sort mappings by score
    _maps.sort(key=lambda x: x[2], reverse=True)
    # Top feature(s), comma separated
    _topfeat = ','.join(t[1] for t in _maps if t[2] == _maps[0][2])
    # Add best feature tag (ZB) to all alignments
    for p in pairs:
        p.set_tag('ZB', _topfeat)

    return _maps



class Telescope(object):
    """

    """
    def __init__(self, opts):

        self.opts = opts               # Command line options
        self.run_info = OrderedDict()  # Information about the run
        # self.annotation = None         # Anntation object
        self.feature_length = None     # Lengths of features
        self.read_index = {}           # {"fragment name": row_index}
        self.feat_index = {}           # {"feature_name": column_index}
        self.shape = None              # Fragments x Features
        self.raw_scores = None         # Initial alignment scores

        # BAM with non overlapping fragments (or unmapped)
        self.other_bam = opts.outfile_path('other.bam')
        # BAM with overlapping fragments
        self.tmp_bam = opts.outfile_path('tmp_tele.bam')

        # Set the version
        self.run_info['version'] = self.opts.version

    def save(self, filename):
        _feat_list = sorted(self.feat_index, key=self.feat_index.get)
        _flen_list = [self.feature_length[f] for f in _feat_list]
        np.savez(filename,
                 _run_info = list(self.run_info.items()),
                 _flen_list = _flen_list,
                 _feat_list = _feat_list,
                 _read_list = sorted(self.read_index, key=self.read_index.get),
                 _shape = self.shape,
                 _raw_scores_data = self.raw_scores.data,
                 _raw_scores_indices=self.raw_scores.indices,
                 _raw_scores_indptr=self.raw_scores.indptr,
                 _raw_scores_shape=self.raw_scores.shape,
                 )

    @classmethod
    def load(cls, filename):
        loader = np.load(filename)
        obj = cls.__new__(cls)
        ''' Run info '''
        obj.run_info = OrderedDict()
        for r in range(loader['_run_info'].shape[0]):
            k = loader['_run_info'][r, 0]
            v = str2int(loader['_run_info'][r, 1])
            obj.run_info[k] = v
        obj.feature_length = Counter()
        for f,fl in zip(loader['_feat_list'], loader['_flen_list']):
            obj.feature_length[f] = fl
        ''' Read and feature indexes '''
        obj.read_index = {n: i for i, n in enumerate(loader['_read_list'])}
        obj.feat_index = {n: i for i, n in enumerate(loader['_feat_list'])}
        obj.shape = len(obj.read_index), len(obj.feat_index)
        assert tuple(loader['_shape']) == obj.shape

        obj.raw_scores = csr_matrix((
            loader['_raw_scores_data'],
            loader['_raw_scores_indices'],
            loader['_raw_scores_indptr'] ),
            shape=loader['_raw_scores_shape']
        )
        return obj

    def load_alignment(self, annotation):
        self.run_info['annotated_features'] = len(annotation.loci)
        self.feature_length = annotation.feature_length().copy()

        _update_sam = self.opts.updated_sam
        _nfkey = self.opts.no_feature_key
        _omode, _othresh = self.opts.overlap_mode, self.opts.overlap_threshold

        _mappings = []
        assign = Assigner(annotation, _nfkey, _omode, _othresh).assign_func()

        """ Load unsorted reads """
        alninfo = Counter()
        with pysam.AlignmentFile(self.opts.samfile) as sf:
            # Create output temporary files
            if _update_sam:
                bam_u = pysam.AlignmentFile(self.other_bam, 'w', template=sf)
                bam_t = pysam.AlignmentFile(self.tmp_bam, 'w', template=sf)

            for pairs in fetch_fragments(sf, until_eof=True):
                alninfo['fragments'] += 1
                if alninfo['fragments'] % 500000 == 0:
                    lg.info('...processed {:.1f}M fragments'.format(alninfo['fragments']/1e6))

                ''' Check whether fragment is mapped '''
                if pairs[0].is_unmapped:
                    alninfo['unmap_{}'.format(pairs[0].numreads)] += 1
                    if _update_sam: pairs[0].write(bam_u)
                    continue

                ''' Fragment is mapped '''
                alninfo['map_{}'.format(pairs[0].numreads)] += 1

                ''' Fragment is ambiguous if multiple mappings'''
                _ambig = len(pairs) > 1

                ''' Check whether fragment overlaps annotation '''
                overlap_feats = list(map(assign, pairs))
                has_overlap = any(f != _nfkey for f in overlap_feats)

                ''' Fragment has no overlap '''
                if not has_overlap:
                    alninfo['nofeat_{}'.format('A' if _ambig else 'U')] += 1
                    if _update_sam:
                        [p.write(bam_u) for p in pairs]
                    continue

                ''' Fragment overlaps with annotation '''
                alninfo['feat_{}'.format('A' if _ambig else 'U')] += 1

                ''' Find the best alignment for each locus '''
                _mappings += process_overlap_frag(pairs, overlap_feats)

                if _update_sam:
                    [p.write(bam_t) for p in pairs]

        ''' Loading complete '''
        self.run_info['total_fragments'] = alninfo['fragments']
        self.run_info['mapped_pairs'] = alninfo['map_2']
        self.run_info['mapped_single'] = alninfo['map_1']
        self.run_info['unmapped'] = alninfo['unmap_2'] + alninfo['unmap_1']
        self.run_info['unique'] = alninfo['nofeat_U'] + alninfo['feat_U']
        self.run_info['ambig'] = alninfo['nofeat_A'] + alninfo['feat_A']
        self.run_info['overlap_unique'] = alninfo['feat_U']
        self.run_info['overlap_ambig'] = alninfo['feat_A']

        if _update_sam:
            bam_u.close()
            bam_t.close()

        self._mapping_to_matrix(_mappings)

    def load_mappings(self, samfile_path):
        _mappings = []
        with pysam.AlignmentFile(samfile_path) as sf:
            for pairs in fetch_fragments(sf, until_eof=True):
                for pair in pairs:
                    if pair.r1.has_tag('ZT') and pair.r1.get_tag('ZT') == 'SEC':
                        continue
                    _mappings.append((
                        pair.query_id,
                        pair.r1.get_tag('ZF'),
                        pair.alnscore,
                        pair.alnlen
                    ))
                    if len(_mappings) % 500000 == 0:
                        lg.info('...loaded {:.1f}M mappings'.format(
                            len(_mappings) / 1e6))
        return _mappings

    def _mapping_to_matrix(self, mappings):
        ''' '''
        _maxAS = max(t[2] for t in mappings)
        _minAS = min(t[2] for t in mappings)

        # Rescale integer alignment score to be greater than zero
        rescale = {s: (s - _minAS + 1) for s in range(_minAS, _maxAS + 1)}

        # Construct dok matrix with mappings
        if 'annotated_features' in self.run_info:
            ncol = self.run_info['annotated_features']
        else:
            ncol = len(set(t[1] for t in mappings))
        dim = (len(mappings), ncol)
        _m1 = scipy.sparse.dok_matrix(dim, dtype=np.uint16)
        _ridx = self.read_index
        _fidx = self.feat_index
        for rid, fid, ascr, alen in mappings:
            i = _ridx.setdefault(rid, len(_ridx))
            j = _fidx.setdefault(fid, len(_fidx))
            _m1[i, j] = max(_m1[i, j], (rescale[ascr] + alen))

        # Trim matrix to size
        _m1 = _m1[:len(_ridx), :len(_fidx)]

        # Convert dok matrix to csr
        self.raw_scores = csr_matrix(_m1)
        self.shape = (len(_ridx), len(_fidx))

    def output_report(self, tl, filename):
        _rmethod, _rprob = self.opts.reassign_mode, self.opts.conf_prob
        _fnames = sorted(self.feat_index, key=self.feat_index.get)
        _flens = self.feature_length
        _final_type = '{:.2f}' if _rmethod in ['average', 'conf'] else '{:d}'
        _dtype = [
            ('transcript', '{:s}'),
            ('transcript_length', '{:d}'),
            ('final_count', _final_type),
            ('final_conf', '{:.2f}'),
            ('final_prop', '{:.3g}'),
            ('init_aligned', '{:d}'),
            ('unique_count', '{:d}'),
            ('init_best', '{:d}'),
            ('init_best_random', '{:d}'),
            ('init_best_avg', '{:.2f}'),
            ('init_prop', '{:.3g}'),
        ]

        _report0 = [
            _fnames,                                       # transcript
            [_flens[f] for f in _fnames],                  # tx_len
            tl.reassign(_rmethod, _rprob).sum(0).A1,       # final_count
            tl.reassign('conf', _rprob).sum(0).A1,         # final_conf
            tl.pi[-1],                                     # final_prop
            tl.reassign('all', iteration=0).sum(0).A1,     # init_aligned
            tl.reassign('unique').sum(0).A1,               # unique_count
            tl.reassign('exclude', iteration=0).sum(0).A1, # init_best
            tl.reassign('choose', iteration=0).sum(0).A1,  # init_best_random
            tl.reassign('average', iteration=0).sum(0).A1, # init_best_avg
            tl.init_pi                                     # init_prop
        ]

        # Rotate the report
        _report = [[r0[i] for r0 in _report0] for i in range(len(_fnames))]

        # Sort the report
        _report.sort(key=lambda x: x[4], reverse=True)
        _report.sort(key=lambda x: x[2], reverse=True)

        _fmtstr = '\t'.join(t[1] for t in _dtype)

        # Run info line
        _comment = ["## RunInfo", ]
        _comment += ['{}:{}'.format(*tup) for tup in self.run_info.items()]

        with open(filename, 'w') as outh:
            print('\t'.join(_comment), file=outh)
            print('\t'.join(t[0] for t in _dtype), file=outh)
            for row in _report:
                print(_fmtstr.format(*row), file=outh)
        return

    def update_sam(self, tl, filename):
        _rmethod, _rprob = self.opts.reassign_mode, self.opts.conf_prob
        _fnames = sorted(self.feat_index, key=self.feat_index.get)

        mat = csr_matrix(tl.reassign(_rmethod, _rprob))
        # best_feats = {i: _fnames for i, j in zip(*mat.nonzero())}

        with pysam.AlignmentFile(self.tmp_bam) as sf:
            header = sf.header
            header['PG'].append({
                'PN': 'telescope', 'ID': 'telescope',
                'VN': self.run_info['version'],
                'CL': ' '.join(sys.argv),
            })
            outsam = pysam.AlignmentFile(filename, 'wb', header=header)
            for pairs in fetch_fragments(sf, until_eof=True):
                if len(pairs) == 0: continue
                ridx = self.read_index[pairs[0].query_id]
                for aln in pairs:
                    if aln.r1.has_tag('ZT'):
                        aln.set_tag('YC', c2str((248, 248, 248)))
                        aln.set_mapq(0)
                    else:
                        fidx = self.feat_index[aln.r1.get_tag('ZF')]
                        prob = tl.z[-1][ridx, fidx]
                        aln.set_tag('XP', int(round(prob*100)))
                        if mat[ridx, fidx] > 0:
                            aln.unset_flag(pysam.FSECONDARY)
                            aln.set_tag('YC',c2str(D2PAL['vermilion']))
                        else:
                            aln.set_flag(pysam.FSECONDARY)
                            if prob >= 0.2:
                                aln.set_tag('YC', c2str(D2PAL['yellow']))
                            else:
                                aln.set_tag('YC', c2str(GPAL[2]))
                    aln.write(outsam)
            outsam.close()

    def print_summary(self, loglev=lg.WARNING):
        _d = self.run_info
        lg.log(loglev, "Alignment Summary:")
        lg.log(loglev, '\t{} total fragments.'.format(_d['total_fragments']))
        lg.log(loglev, '\t\t{} mapped as pairs.'.format(_d['mapped_pairs']))
        lg.log(loglev, '\t\t{} mapped single.'.format(_d['mapped_single']))
        lg.log(loglev, '\t\t{} failed to map.'.format(_d['unmapped']))
        lg.log(loglev, '--')
        lg.log(loglev, '\t{} fragments mapped to reference; of these'.format(
            _d['mapped_pairs'] + _d['mapped_single']))
        lg.log(loglev, '\t\t{} had one unique alignment.'.format(_d['unique']))
        lg.log(loglev, '\t\t{} had multiple alignments.'.format(_d['ambig']))
        lg.log(loglev, '--')
        lg.log(loglev, '\t{} fragments overlapped annotation; of these'.format(
            _d['overlap_unique'] + _d['overlap_ambig']))
        lg.log(loglev, '\t\t{} had one unique alignment.'.format(
            _d['overlap_unique']))
        lg.log(loglev, '\t\t{} had multiple alignments.'.format(
            _d['overlap_ambig']))
        lg.log(loglev, '\n')

    def __str__(self):
        if hasattr(self.opts, 'samfile'):
            return '<Telescope samfile=%s, gtffile=%s>'.format(
                self.opts.samfile, self.opts.gtffile)
        elif hasattr(self.opts, 'checkpoint'):
            return '<Telescope checkpoint=%s>'.format(self.opts.checkpoint)
        else:
            return '<Telescope>'

class TelescopeLikelihood(object):
    """

    """
    def __init__(self, score_matrix, opts):
        """
        """
        # Raw scores
        self.raw_scores = score_matrix
        self.max_score = self.raw_scores.max()

        # N fragments x K transcripts
        self.N, self.K = self.raw_scores.shape

        # Q[i,] is the set of mapping qualities for fragment i, where Q[i,j]
        # represents the evidence for fragment i being generated by fragment j.
        # In this case the evidence is represented by an alignment score, which
        # is greater when there are more matches and is penalized for
        # mismatches
        # Scale the raw alignment score by the maximum alignment score
        # and multiply by a scale factor.
        self.scale_factor = 100.
        self.Q = self.raw_scores.scale().multiply(self.scale_factor).expm1()

        # z[i,] is the partial assignment weights for fragment i, where z[i,j]
        # is the expected value for fragment i originating from transcript j. The
        # initial estimate is the normalized mapping qualities:
        # z_init[i,] = Q[i,] / sum(Q[i,])
        self.z = [ self.Q.norm(1), ]

        self.epsilon = opts.em_epsilon
        self.max_iter = opts.max_iter

        # pi[j] is the proportion of fragments that originate from
        # transcript j. Initial value assumes that all transcripts contribute
        # equal proportions of fragments
        self.pi = [ np.repeat(1./self.K, self.K), ]
        self.init_pi = None

        # theta[j] is the proportion of non-unique fragments that need to be
        # reassigned to transcript j. Initial value assumes that all transcripts
        # are reassigned an equal proportion of fragments
        self.theta = [ np.repeat(1./self.K, self.K), ]

        # Y[i] is the ambiguity indicator for fragment i, where Y[i]=1 if
        # fragment i is aligned to multiple transcripts and Y[i]=0 otherwise.
        # Store as N x 1 matrix
        self.Y = (self.Q.count(1) > 1).astype(np.int)

        # Log-likelihood score
        self.lnl = [float('inf'), ]

        # Prior values
        self.pi_prior = opts.pi_prior
        self.theta_prior = opts.theta_prior

        # Precalculated values
        self._weights = self.Q.max(1)             # Weight assigned to each fragment
        self._total_wt = self._weights.sum()      # Total weight
        self._ambig_wt = self._weights.multiply(self.Y).sum() # Weight of ambig frags
        self._unique_wt = self._weights.multiply(1-self.Y).sum()

        # Weighted prior values
        self._pi_prior_wt = self.pi_prior * self._weights.max()
        self._theta_prior_wt = self.theta_prior * self._weights.max()
        #
        self._pisum0 = self.Q.multiply(1-self.Y).sum(0)
        lg.debug('done initializing model')

    def estep(self):
        """ Calculate the expected values of z
                E(z[i,j]) = ( pi[j] * theta[j]**Y[i] * Q[i,j] ) /
        """
        # assert len(self.z) == len(self.pi) == len(self.theta)
        lg.debug('started e-step')
        _pi = self.pi[-1]
        _theta = self.theta[-1]

        # Old way:
        # _numerator = self.Q.multiply(csr_matrix(_pi * (_theta ** self.Y)))

        # New way:
        _n = self.Q.copy()
        _rowiter = zip(_n.indptr[:-1], _n.indptr[1:], self.Y[:, 0])
        for d_start, d_end, indicator in _rowiter:
            _cidx = _n.indices[d_start:d_end]
            if indicator == 1:
                _n.data[d_start:d_end] *= (_pi[_cidx] * _theta[_cidx])
            else:
                _n.data[d_start:d_end] *= _pi[_cidx]

        self.z.append(_n.norm(1))

    def mstep(self):
        """ Calculate the maximum a posteriori (MAP) estimates for pi and theta

        """
        # assert (len(self.z)-1) == len(self.pi) == len(self.theta)
        lg.debug('started m-step')
        # The expected values of z weighted by mapping score
        _weighted = self.z[-1].multiply(self._weights)

        # Estimate theta_hat
        _thetasum = _weighted.multiply(self.Y).sum(0)
        _theta_denom = self._ambig_wt + self._theta_prior_wt * self.K
        _theta_hat = (_thetasum + self._theta_prior_wt) / _theta_denom

        # Estimate pi_hat
        _pisum = self._pisum0 + _thetasum
        # _pi_denom = self._ambig_wt + self._unique_wt + self._pi_prior_wt * self.K
        _pi_denom = self._total_wt + self._pi_prior_wt * self.K
        _pi_hat = (_pisum + self._pi_prior_wt) / _pi_denom

        self.theta.append(_theta_hat.A1)
        self.pi.append(_pi_hat.A1)

    def calculate_lnl(self):
        lg.debug('started lnl')
        _z, _p, _t = self.z[-1], self.pi[-1], self.theta[-1]

        # Old way
        # old_cur = _z.multiply(self.Q.multiply(_p * _t**self.Y).log1p()).sum()

        # New way
        _inner = self.Q.copy()
        _rowiter = zip(_inner.indptr[:-1], _inner.indptr[1:], self.Y[:, 0])
        for d_start, d_end, indicator in _rowiter:
            _cidx =  _inner.indices[d_start:d_end]
            if indicator == 1:
                _inner.data[d_start:d_end] *= (_p[_cidx] * _t[_cidx])
            else:
                _inner.data[d_start:d_end] *= _p[_cidx]
        cur = _z.multiply(_inner.log1p()).sum()
        lg.debug('completed lnl')
        return cur

    def em(self, use_likelihood=False, loglev=lg.WARNING, save_memory=True):
        inum = 0               # Iteration number
        converged = False      # Has convergence been reached?
        reached_max = False    # Has max number of iterations been reached?
        msgD = 'Iteration {:d}, diff={:.5g}'
        msgL = 'Iteration {:d}, lnl= {:.5e}, diff={:.5g}'

        while not (converged or reached_max):
            self.estep()
            self.mstep()
            inum += 1
            if inum == 1: self.init_pi = self.pi[1]

            ''' Calculate absolute difference between estimates '''
            diff_est = abs(self.pi[-1] - self.pi[-2]).sum()

            if use_likelihood:
                ''' Calculate likelihood '''
                self.lnl.append( self.calculate_lnl() )
                diff_lnl = abs(self.lnl[-1] - self.lnl[-2])
                lg.log(loglev, msgL.format(inum, self.lnl[-1], diff_est))
                converged = diff_lnl < self.epsilon
            else:
                lg.log(loglev, msgD.format(inum, diff_est))
                converged = diff_est < self.epsilon

            reached_max = inum >= self.max_iter
            if save_memory:
                print(len(self.z))
                assert len(self.z) == len(self.pi)
                assert len(self.z) == len(self.theta)
                self.z = [self.z[0], self.z[-1]]
                self.pi = [self.pi[0], self.pi[-1]]
                self.theta = [self.theta[0], self.theta[-1]]
                lg.debug('garbage: {:d}'.format(gc.collect()))

        _con = 'converged' if converged else 'terminated'
        if not use_likelihood: self.lnl.append(self.calculate_lnl())

        lg.log(loglev, 'EM {:s} after {:d} iterations.'.format(_con, inum))
        lg.log(loglev, 'Final log-likelihood: {:f}.'.format(self.lnl[-1]))
        return

    def reassign(self, method, thresh=0.9, iteration=-1):
        """ Reassign fragments to expected transcripts

        Running EM finds the expected fragment assignment weights at the MAP
        estimates of pi and theta. This function reassigns all fragments based
        on these assignment weights. A simple heuristic is to assign each
        fragment to the transcript with the highest assignment weight.

        In practice, not all fragments have exactly one best hit. The "method"
        argument defines how we deal with fragments that are not fully resolved
        after EM:
                exclude - reads with > 1 best hits are excluded
                choose  - one of the best hits is randomly chosen
                average - read is evenly divided among best hits
                conf    - only confident reads are reassigned
                unique  - only uniquely aligned reads
        Args:
            method:
            thresh:
            iteration:

        Returns:
            matrix where m[i,j] == 1 iff read i is reassigned to transcript j

        """
        _z = self.z[iteration]
        if method == 'exclude':
            # Identify best hit(s), then exclude rows with >1 best hits
            v = _z.binmax(1)
            return v.multiply(v.sum(1) == 1)
        elif method == 'choose':
            # Identify best hit(s), then randomly choose reassignment
            v = _z.binmax(1)
            return v.choose_random(1)
        elif method == 'average':
            # Identify best hit(s), then divide by row sum
            v = _z.binmax(1)
            return v.norm(1)
        elif method == 'conf':
            # Zero out all values less than threshold
            # If thresh > 0.5 then at most
            v = _z.apply_func(lambda x: x if x >= thresh else 0)
            # Average each row so each sums to 1.
            return v.norm(1)
        elif method == 'unique':
            # Zero all rows that are ambiguous
            return _z.multiply(1 - self.Y).ceil().astype(np.uint8)
        elif method == 'all':
            # Return all nonzero elements
            return _z.apply_func(lambda x: 1 if x > 0 else 0).astype(np.uint8)


class Assigner:
    def __init__(self, annotation,
                 no_feature_key, overlap_mode, overlap_threshold):
        self.annotation = annotation
        self.no_feature_key = no_feature_key
        self.overlap_mode = overlap_mode
        self.overlap_threshold = overlap_threshold

    def assign_func(self):
        def _assign_pair_threshold(pair):
            blocks = pair.refblocks
            f = self.annotation.intersect_blocks(pair.ref_name, blocks)
            if not f:
                return self.no_feature_key
            # Calculate the percentage of fragment mapped
            fname, overlap = f.most_common()[0]
            if overlap > pair.alnlen * self.overlap_threshold:
                return fname
            else:
                return self.no_feature_key

        def _assign_pair_intersection_strict(pair):
            pass

        def _assign_pair_union(pair):
            pass

        ''' Return function depending on overlap mode '''
        if self.overlap_mode == 'threshold':
            return _assign_pair_threshold
        elif self.overlap_mode == 'intersection-strict':
            return _assign_pair_intersection_strict
        elif self.overlap_mode == 'union':
            return _assign_pair_union
        else:
            assert False