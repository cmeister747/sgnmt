# -*- coding: utf-8 -*-
# coding=utf-8
# Copyright 2019 The SGNMT Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This module encapsulates the predictor interface to OpenFST. This
module depends on OpenFST. To enable Python support in OpenFST, use a 
recent version (>=1.5.4) and compile with ``--enable_python``. 
Further information can be found here:

http://www.openfst.org/twiki/bin/view/FST/PythonExtension 

This file includes the fst, nfst, and rtn predictors.

Note: If we use arc weights in FSTs, we multiply them by -1 as 
everything in SGNMT is logprob, not -logprob as in FSTs log 
or tropical semirings. You can disable this behavior with --fst_to_log

Note2: The FSTs and RTNs are assumed to have both <S> and </S>. This 
has compatibility reasons, as lattices generated by HiFST have these
symbols.
"""

import glob
import logging
import os
import sys

from cam.sgnmt import utils
from cam.sgnmt.predictors.core import Predictor
from cam.sgnmt.utils import w2f, load_fst

try:
    import pywrapfst as fst
except ImportError:
    try:
        import openfst_python as fst
    except ImportError:
        pass # Deal with it in decode.py


EPS_ID = 0
"""OpenFST's reserved ID for epsilon arcs. """


class FstPredictor(Predictor):
    """This predictor can read determinized translation lattices. The
    predictor state consists of the current node. This is unique as the
    lattices are determinized.
    """
    
    def __init__(self,
                 fst_path,
                 use_weights,
                 normalize_scores,
                 skip_bos_weight = True,
                 to_log = True):
        """Creates a new fst predictor.
        
        Args:
            fst_path (string): Path to the FST file
            use_weights (bool): If false, replace all arc weights with
                                0 (=log 1).
            normalize_scores (bool): If true, we normalize the weights
                                     on all outgoing arcs such that
                                     they sum up to 1
            skip_bos_weight (bool): Add the score at the <S> arc to the
                                    </S> arc if this is false. This results
                                    in scores consistent with 
                                    OpenFST's replace operation,
                                    as <S> scores are normally
                                    ignored by SGNMT.
            to_log (bool): SGNMT uses normal log probs (scores) while
                           arc weights in FSTs normally have cost (i.e.
                           neg. log values) semantics. Therefore, if
                           true, we multiply arc weights by -1.
        """
        super(FstPredictor, self).__init__()
        self.fst_path = fst_path
        self.weight_factor = -1.0 if to_log else 1.0
        self.use_weights = use_weights
        self.normalize_scores = normalize_scores
        self.cur_fst = None
        self.add_bos_to_eos_score = not skip_bos_weight
        self.cur_node = -1
        
    def get_unk_probability(self, posterior):
        """Returns negative infinity if UNK is not in the lattice.
        Otherwise, return UNK score.
        
        Returns:
            float. Negative infinity
        """
        return utils.common_get(posterior, utils.UNK_ID, utils.NEG_INF)
    
    def predict_next(self):
        """Uses the outgoing arcs from the current node to build up the
        scores for the next word.
        
        Returns:
            dict. Set of words on outgoing arcs from the current node
            together with their scores, or an empty set if we currently
            have no active node or fst.
        """
        if self.cur_node < 0:
            return {}
        scores = {arc.olabel: self.weight_factor*w2f(arc.weight)
                for arc in self.cur_fst.arcs(self.cur_node)}
        if utils.EOS_ID in scores and self.add_bos_to_eos_score:
            scores[utils.EOS_ID] += self.bos_score
        return self.finalize_posterior(scores,
                self.use_weights, self.normalize_scores)
    
    def initialize(self, src_sentence):
        """Loads the FST from the file system and consumes the start
        of sentence symbol. 
        
        Args:
            src_sentence (list):  Not used
        """
        self.cur_fst = load_fst(utils.get_path(self.fst_path,
                                               self.current_sen_id+1))
        self.cur_node = self.cur_fst.start() if self.cur_fst else None
        self.bos_score = self.consume(utils.GO_ID)
        if not self.bos_score: # Override None
            self.bos_score = 0.0
        if self.cur_node is None:
            logging.warn("The lattice for sentence %d does not contain any "
                         "valid path. Please double-check that the lattice "
                         "is not empty and that paths contain the begin-of-"
                         "sentence symbol %d. If you are using a different "
                         "begin-of-sentence symbol, double-check --indexing_"
                         "scheme." % (self.current_sen_id+1, utils.GO_ID))
    
    def consume(self, word):
        """Updates the current node by following the arc labelled with
        ``word``. If there is no such arc, we set ``cur_node`` to -1,
        indicating that the predictor is in an invalid state. In this
        case, all subsequent ``predict_next`` calls will return the
        empty set.
        
        Args:
            word (int): Word on an outgoing arc from the current node
        
        Returns:
            float. Weight on the traversed arc
        """
        if self.cur_node < 0:
            return
        from_state = self.cur_node
        self.cur_node = None
        unk_arc = None
        for arc in self.cur_fst.arcs(from_state):
            if arc.olabel == word:
                self.cur_node = arc.nextstate
                return self.weight_factor*w2f(arc.weight)
            elif arc.olabel == utils.UNK_ID:
                unk_arc = arc
        if unk_arc is not None:
            self.cur_node = unk_arc.nextstate
    
    def get_state(self):
        """Returns the current node. """
        return self.cur_node
    
    def set_state(self, state):
        """Sets the current node. """
        self.cur_node = state

    def initialize_heuristic(self, src_sentence):
        """Creates a matrix of shortest distances between nodes. """
        self.distances = fst.shortestdistance(self.cur_fst, reverse=True)
    
    def estimate_future_cost(self, hypo):
        """The FST predictor comes with its own heuristic function. We
        use the shortest path in the fst as future cost estimator. """
        if not self.cur_node:
            return 0.0
        last_word = hypo.trgt_sentence[-1]
        for arc in self.cur_fst.arcs(self.cur_node):
            if arc.olabel == last_word:
                return w2f(self.distances[arc.nextstate])
        return 0.0
    
    def is_equal(self, state1, state2):
        """Returns true if the current node is the same """
        return state1 == state2


class NondeterministicFstPredictor(Predictor):
    """This predictor can handle non-deterministic translation 
    lattices. In contrast to the fst predictor for deterministic
    lattices, we store a set of nodes which are all reachable from
    the start node through the current history.
    """
    
    def __init__(self, 
                 fst_path, 
                 use_weights, 
                 normalize_scores, 
                 skip_bos_weight = True, 
                 to_log = True):
        """Creates a new nfst predictor.
        
        Args:
            fst_path (string): Path to the FST file
            use_weights (bool): If false, replace all arc weights with
                                0 (=log 1).
            normalize_scores (bool): If true, we normalize the weights
                                     on all outgoing arcs such that
                                     they sum up to 1
            skip_bos_weight (bool): If true, set weights on <S> arcs
                                    to 0 (= log1)
            to_log (bool): SGNMT uses normal log probs (scores) while
                           arc weights in FSTs normally have cost (i.e.
                           neg. log values) semantics. Therefore, if
                           true, we multiply arc weights by -1.
        """
        super(NondeterministicFstPredictor, self).__init__()
        self.fst_path = fst_path
        self.weight_factor = -1.0 if to_log else 1.0
        self.score_max_func = max if to_log else min
        self.use_weights = use_weights
        self.skip_bos_weight = skip_bos_weight
        self.normalize_scores = normalize_scores
        self.cur_fst = None
        self.cur_nodes = []
        
    def get_unk_probability(self, posterior):
        """Always returns negative infinity: Words outside the 
        translation lattice are not possible according to this
        predictor.
        
        Returns:
            float. Negative infinity
        """
        return utils.NEG_INF 
    
    def predict_next(self):
        """Uses the outgoing arcs from all current node to build up the
        scores for the next word. This method does not follow epsilon
        arcs: ``consume`` updates ``cur_nodes`` such that all reachable
        arcs with word ids are connected directly with a node in
        ``cur_nodes``. If there are multiple arcs with the same word,
        we use the log sum of the arc weights as score.
        
        Returns:
            dict. Set of words on outgoing arcs from the current node
            together with their scores, or an empty set if we currently
            have no active nodes or fst.
        """
        scores = {}
        for weight,node in self.cur_nodes:
            for arc in self.cur_fst.arcs(node): 
                if arc.olabel != EPS_ID:
                    score = weight + self.weight_factor*w2f(arc.weight) 
                    if arc.olabel in scores:
                        scores[arc.olabel] = self.score_max_func(
                                        scores[arc.olabel], score)
                    else:
                        scores[arc.olabel] = score 
        return self.finalize_posterior(scores,
                self.use_weights, self.normalize_scores)
    
    def initialize(self, src_sentence):
        """Loads the FST from the file system and consumes the start
        of sentence symbol. 
        
        Args:
            src_sentence (list):  Not used
        """
        self.cur_fst = load_fst(utils.get_path(self.fst_path,
                                               self.current_sen_id+1))
        self.cur_nodes = []
        if self.cur_fst:
            self.cur_nodes = self._follow_eps({self.cur_fst.start(): 0.0})
        self.consume(utils.GO_ID)
        if not self.cur_nodes:
            logging.warn("The lattice for sentence %d does not contain any "
                         "valid path. Please double-check that the lattice "
                         "is not empty and that paths start with the begin-of-"
                         "sentence symbol." % (self.current_sen_id+1))
    
    def consume(self, word):
        """Updates the current nodes by searching for all nodes which
        are reachable from the current nodes by a path consisting of 
        any number of epsilons and exactly one ``word`` label. If there
        is no such arc, we set the predictor in an invalid state. In 
        this case, all subsequent ``predict_next`` calls will return 
        the empty set.
        
        Args:
            word (int): Word on an outgoing arc from the current node
        """
        d_unconsumed = {}
        # Collect distances to nodes reachable by word
        for weight,node in self.cur_nodes:
            for arc in self.cur_fst.arcs(node):
                if arc.olabel == word:
                    next_node = arc.nextstate
                    next_score = weight + self.weight_factor*w2f(arc.weight)
                    if d_unconsumed.get(next_node, utils.NEG_INF) < next_score:
                        d_unconsumed[next_node] = next_score
        # Subtract the word score from the last predict_next 
        consumed_score = self.score_max_func(d_unconsumed.values()) \
             if (word != utils.GO_ID or self.skip_bos_weight) else 0.0
        # Add epsilon reachable states
        self.cur_nodes = self._follow_eps({node: score - consumed_score
                    for node,score in d_unconsumed.items()})
    
    def _follow_eps(self, roots):
        """BFS to find nodes reachable from root through eps arcs. This
        traversal strategy is efficient if the triangle inquality holds 
        for weights in the graphs, i.e. for all vertices v1,v2,v3: 
        (v1,v2),(v2,v3),(v1,v3) in E => d(v1,v2)+d(v2,v3) >= d(v1,v3).
        The method still returns the correct results if the triangle
        inequality does not hold, but edges may be traversed multiple
        times which makes it more inefficient.
        """
        open_nodes = dict(roots)
        d = {}
        visited = dict(roots)
        while open_nodes:
            next_open = {}
            for node,score in open_nodes.items():
                has_noneps = False
                for arc in self.cur_fst.arcs(node):
                    if arc.olabel == EPS_ID:
                        next_node = arc.nextstate
                        next_score = score + self.weight_factor*w2f(arc.weight)
                        if visited.get(next_node, utils.NEG_INF) < next_score:
                            visited[next_node] = next_score
                            next_open[next_node] = next_score
                    else:
                        has_noneps = True
                if has_noneps:
                    d[node] = score
            open_nodes = next_open
        return [(weight, node) for node, weight in d.items()]
        
    def get_state(self):
        """Returns the set of current nodes """
        return self.cur_nodes
    
    def set_state(self, state):
        """Sets the set of current nodes """
        self.cur_nodes = state

    def initialize_heuristic(self, src_sentence):
        """Creates a matrix of shortest distances between all nodes """
        self.distances = fst.shortestdistance(self.cur_fst, reverse=True)
    
    def estimate_future_cost(self, hypo):
        """The FST predictor comes with its own heuristic function. We
        use the shortest path in the fst as future cost estimator. """
        last_word = hypo.trgt_sentence[-1]
        dists = []
        for n in self.cur_nodes:
            for arc in self.cur_fst[n].arcs:
                if arc.olabel == last_word:
                    dists.append(w2f(self.distances[arc.nextstate]))
                    break
        return 0.0 if not dists else min(dists)
    
    def is_equal(self, state1, state2):
        """Returns true if the current nodes are the same """
        return sorted([n for _,n in state1]) == sorted([n for _,n in state2])


class RtnPredictor(Predictor):
    """Predictor for RTNs (recurrent transition networks). This 
    predictor assumes a directory structure as produced by HiFST. You 
    can use this predictor for non-deterministic lattices too. This
    implementation supports late expansion: RTNs are only expanded as
    far as necessary to retrieve all currently reachable states.
    
    ``cur_nodes`` contains the accumulated weights from the last 
    consumed word (if ambiguous, the largest)
    
    This implementation does not maintain a list of active nodes like 
    the other automata predictors. Instead, we store the current 
    history and search for the active nodes at each expansion. This is
    more expensive, but fstreplace might change state IDs so a list of
    active nodes might get corrupted.
    
    Note that this predictor does not support FSTs in gzip format.
    """
    
    def __init__(self,
                 rtn_path,
                 use_weights,
                 normalize_scores,
                 to_log = True,
                 minimize_rtns = False,
                 rmeps = True):
        """Creates a new RTN predictor.
        
        Args:
            rtn_path (string): Path to the RTN directory
            use_weights (bool): If false, replace all arc weights with
                                0 (=log 1).
            normalize_scores (bool): If true, we normalize the weights
                                     on all outgoing arcs such that
                                     they sum up to 1
            to_log (bool): SGNMT uses normal log probs (scores) while
                           arc weights in FSTs normally have cost (i.e.
                           neg. log values) semantics. Therefore, if
                           true, we multiply arc weights by -1.
            minimize_rtns (bool): Minimize the FST after each replace
                                  operation
            rmeps (bool): Remove epsilons in the FST after each replace
                          operation 
        """
        super(RtnPredictor, self).__init__()
        self.root_path = rtn_path
        self.minimize_rtns = minimize_rtns
        self.rmeps = rmeps
        self.use_weights = use_weights
        self.normalize_scores = normalize_scores
        self.weight_factor = -1.0 if to_log else 1.0
        self.cur_fst = None # current root fst
        start_id = '1'
        try:
            with open("%s/ntmap" % self.root_path) as f:
                ntmap = dict(line.strip().split(None, 1) for line in f)
                start_id = ntmap['S']
        except:
            logging.warn("Could not find NT S in ntmap. Assuming its ID 1")
        self.root_fst_prefix = "1%s000" % start_id.zfill(3)
        
    def get_unk_probability(self, posterior):
        """Always returns negative infinity: Words outside the 
        RTN are not possible according to this predictor.
        
        Returns:
            float. Negative infinity
        """
        return utils.NEG_INF
    
    def initialize(self, src_sentence):
        """Loads the root RTN and consumes the start of sentence 
        symbol.
        
        Args:
            src_sentence (list):  Not used
        """
        try:
            file_name = "%s/%d.fst" % (self.root_path, self.current_sen_id+1)
            if not os.access(file_name, os.R_OK): # Find root FST
                search_pattern = '%s/%d/%s*.fst' % (self.root_path,
                                                    self.current_sen_id+1,
                                                    self.root_fst_prefix)
                candidates = glob.glob(search_pattern)
                if not candidates:
                    logging.error("Could not find root fst in %s" % 
                                    search_pattern)
                    self.cur_fst = None
                    return
                if len(candidates) > 1:
                    logging.warn("Ambiguous root fst for %s. Take the one "
                                 "with largest span." % search_pattern)
                    candidates = sorted(candidates)
                file_name = candidates[-1]
            self.cur_fst = fst.Fst.read(file_name) 
            logging.debug("Read (root)fst from %s" % file_name)
        except Exception as e:
            logging.error("%s error reading fst from %s: %s" %
                (sys.exc_info()[1], file_name, e))
            self.cur_fst = None
        finally:
            self.cur_history = []
            self.sub_fsts = {}
        self.consume(utils.GO_ID)
    
    def expand_rtn(self, func):
        """This method expands the RTN as far as necessary. This means
        that the RTN is expanded s.t. we can build the posterior for 
        ``cur_history``. In practice, this means that we follow all 
        epsilon edges and replaces all NT edges until all paths with 
        the prefix ``cur_history`` in the RTN have at least one more 
        terminal token. Then, we apply ``func`` to all reachable nodes.
        """
        updated = True
        while updated:
            updated = False
            label_fst_map = {}
            self.visited_nodes = {}
            self.cur_fst.arcsort(sort_type="olabel")
            self.add_to_label_fst_map_recursive(label_fst_map,
                                                {},
                                                self.cur_fst.start(), 
                                                0.0,
                                                self.cur_history, func)
            if label_fst_map:
                logging.debug("Replace %d NT arcs for history %s" % (
                                                            len(label_fst_map),
                                                            self.cur_history))
                # First in the list is the root FST and label
                replaced_fst = fst.replace(
                        [(len(label_fst_map) + 2000000000, self.cur_fst)] 
                        + [(nt_label, f) 
                            for (nt_label, f) in label_fst_map.items()],
                        epsilon_on_replace=True)
                self.cur_fst = replaced_fst
                updated = True
        if self.rmeps or self.minimize_rtns:
            self.cur_fst.rmepsilon()
        if self.minimize_rtns:
            tmp = fst.determinize(self.cur_fst.determinize)
            self.cur_fst = tmp
            self.cur_fst.minimize()
    
    def add_to_label_fst_map_recursive(self, 
                                       label_fst_map, 
                                       visited_nodes, 
                                       root_node, 
                                       acc_weight, 
                                       history, 
                                       func):
        """Adds arcs to ``label_fst_map`` if they are labeled with an
        NT symbol and reachable from ``root_node`` via ``history``.
          
        Note: visited_nodes is maintained for each history separately
        """
        if root_node in visited_nodes:
            # This introduces some error as we take the score of the first best
            # path with a certain history, not the globally best path. For now,
            # this error should not be significant
            return
        visited_nodes[root_node] = True
        for arc in self.cur_fst.arcs(root_node):
            arc_acc_weight = acc_weight + self.weight_factor*w2f(arc.weight)
            if arc.olabel == EPS_ID: # Follow epsilon edges
                self.add_to_label_fst_map_recursive(label_fst_map,
                                                    visited_nodes,
                                                    arc.nextstate,
                                                    arc_acc_weight, 
                                                    history,
                                                    func)
            elif not history:
                if self.is_nt_label(arc.olabel): # Add to label_fst_map
                    replace_label = len(label_fst_map) + 2000000000
                    label_fst_map[replace_label] = self.get_sub_fst(
                                                                    arc.olabel)
                    arc.ilabel = replace_label
                    arc.olabel = replace_label
                else: # This is a regular arc and we have no history left
                    func(arc.nextstate, arc.olabel, arc_acc_weight) # apply func
            elif arc.olabel == history[0]: # history is not empty
                self.add_to_label_fst_map_recursive(label_fst_map,
                                                    {},
                                                    arc.nextstate,
                                                    arc_acc_weight,
                                                    history[1:],
                                                    func)
            elif arc.olabel > history[0]: # FST is arc sorted, we can stop here
                break
        
    
    def is_nt_label(self, label):
        """Returns true if ``label`` is a non-terminal. """
        s = str(label)
        return len(s) == 10 and s[0] == '1'

    def get_sub_fst(self, fst_id):
        """ Load sub fst from the file system or the cache """
        if fst_id in self.sub_fsts:
            return self.sub_fsts[fst_id]
        sub_fst_path = "%s/%d/%d.fst" %  (self.root_path,
                                          self.current_sen_id+1,
                                          fst_id)
        try:
            sub_fst = fst.Fst.read(sub_fst_path)
            logging.debug("Read sub fst from %s" % sub_fst_path)
            self.sub_fsts[fst_id] = sub_fst
            return sub_fst
        except Exception as e:
            logging.error("%s error reading sub fst from %s: %s" %
                (sys.exc_info()[1], sub_fst_path, e))
        
    def _add_to_cur_posterior(self, node, label, weight):
        """Can be used as ``func`` argument in ``expand_rtn`` to build
        up the posterior for the next target token  in ``predict_next``
        """
        self.cur_posterior[label] = max(self.cur_posterior.get(label, utils.NEG_INF),
                                        weight)
    
    def predict_next(self):
        """Expands RTN as far as possible and uses the outgoing edges 
        from nodes reachable by the current history to build up
        the posterior for the next word. If there are no such nodes
        or arcs, or no root FST is loaded, return the empty set.
        """
        if not self.cur_fst:
            return {}
        self.cur_posterior = {}
        self.expand_rtn(self._add_to_cur_posterior)
        return self.finalize_posterior(self.cur_posterior,
                                       self.use_weights,
                                       self.normalize_scores)
    
    def consume(self, word):
        """Adds ``word`` to the current history. """
        self.cur_history.append(word)
    
    def get_state(self):
        """Returns the current history. """
        return self.cur_history
    
    def set_state(self, state):
        """Sets the current history. """
        self.cur_history = state

