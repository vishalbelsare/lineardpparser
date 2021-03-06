import sys
logs = sys.stderr

import model
from collections import defaultdict

import gflags as flags
FLAGS=flags.FLAGS

from deptree import DepTree, DepVal

class NewState(object):
    '''stack is a list of DepTrees.
       status=0/1/2=SH/LR/RR.
       score is forward (accumulative) cost.
       step is the number of steps.
       [i,j] span boundaries (next word is sent[j]).
       feats is features before concatenation with LEFT/RIGHT/SHIFT.
       leftptrs is the left pointers (states to my left that can combine w/ me): graph-structured stack.
       inside is the inside viterbi cost.
    '''

    names = ["SHIFT", "LEFT", "RIGHT"]
    mapnames = {"SHIFT":0, "LEFT":1, "RIGHT":2}
    
    __slots__ = "i", "j", "score", "action", "step", "_feats", "_signature", \
                "leftptrs", "shiftcost", "inside", "_hash", "rank", "backptrs", "gold", \
                "s0", "s1", "s2", "q0", "q1", "q2", "s0lcrc", "s1lcrc", "nodeid", "headidx"

    sent = None ## cached for convenience (online use only)
    featscache = defaultdict(list)
    actionfeatscache = defaultdict(lambda :model.new_vector())

    shared = 0
    tot = 0

    @staticmethod
    def setup():
##        NewState.featscache = defaultdict(list)
        NewState.actionfeatscache = {} #defaultdict(lambda :model.new_vector())

    def __init__(self, step=0, i=0, j=0, action=0):
        self._signature = None
        self._feats = None
        self._hash = None
        self.step = step
        self.j = j
        self.i = i
        self.rank = -1
        self.action = action

    @staticmethod
    def initstate():
        s = NewState()
        s.gold = True
        s.score = 0
        s.inside = 0
        s.shiftcost = 0
        s.s0 = s.s1 = s.s2 = ("<s>", "<s>")
        s.q0 = s.q(0)
        s.q1 = s.q(1)
        s.q2 = s.q(2)
        s.s0lcrc = ("NONE", "NONE")
        s.s1lcrc = ("NONE", "NONE")
        s.leftptrs = None
        s.backptrs = None
        return s

    def __cmp__(self, other):
        c = cmp(other.score, self.score)  # opt=MAX; N.B. must use cmp(a,b), can't use return a - b!!
        return c if c != 0 else cmp(other.inside, self.inside)

    def allowed_actions(self):
        ''' returns the set of allowed actions for the current state. '''
        a = []
        if self.j < len(self.sent):
            a += [0]
        if self.s1[1] != "<s>": # not enough to REDUCE: only SHIFT
            a += [1,2]
        return a

    def unk(self, (w, t)):
        if self.model.unk and w not in self.model.knowns:
            w = t if self.model.unktag else "<unk>"
        return w, t
    
    def q(self, j=0):
        j += self.j
        return self.sent[j] if j < len(self.sent) else ("</s>", "</s>")
##        return self.unk(wt)

    def shift(self):
        new = NewState(self.step+1, self.j, self.j+1, 0)
        new.headidx = self.j # TODO
##        new.s0 = self.unk(self.sent[self.j])
        new.s0 = self.sent[self.j]
        new.s1 = self.s0
        new.s2 = self.s1
        new.q0 = self.q1
        new.q1 = self.q2
        new.q2 = self.q(3)
        new.s0lcrc = ("NONE", "NONE")
        new.s1lcrc = self.s0lcrc

        return new

    def reduce(self, left, action):
        new = NewState(self.step+1, left.i, self.j, action)
        new.q0 = self.q0
        new.q1 = self.q1
        new.q2 = self.q2        
        new.s1 = left.s1 # not self.s2!
        new.s2 = left.s2
        new.s0 = self.s0 if action == 1 else left.s0 # not self.s1!
        new.headidx = self.headidx if action == 1 else left.headidx # TODO
        new.s0lcrc = (left.s0[1], self.s0lcrc[1]) if action == 1 else \
                     (left.s0lcrc[0], self.s0[1])
        new.s1lcrc = left.s1lcrc

        return new
        
    def take(self, action, action_gold=False):
        '''returns a list (iterator) of resulting states.'''

        if self.i == self.j == 0: ## don't count start
            actioncost = 0
        else:
##            actioncost = self.feats(action).dot(self.model.weights) if self.model is not None else 0            
            actioncost = self.featscore(action) if self.model is not None else 0            
            
        if action == 0:  # SHIFT
            new = self.shift()
            
            new.inside = 0
            self.shiftcost = actioncost # N.B.: self!
            new.score = self.score + actioncost # forward cost
            new.leftptrs = [self]
            new.backptrs = [(None, action, 0)] # shift has no children

            new.gold = self.gold and action_gold   # gold is sequentially incremented

            yield new
            
        else:  # REDUCE
            for leftstate in self.leftptrs: # i'm combining with it
                new = self.reduce(leftstate, action)
                
                new.inside = leftstate.inside + self.inside + \
                             leftstate.shiftcost + actioncost # N.B.
        
                new.score = leftstate.score + self.inside + leftstate.shiftcost + actioncost #n.b.
                new.leftptrs = leftstate.leftptrs
                new.backptrs = [((leftstate, self), action, leftstate.shiftcost + actioncost)]

                new.gold = leftstate.gold and self.gold and action_gold   # gold is binary
                
                yield new

    def signature(self):
        # TODO: for joint tagging: remove the [0]
        if self._signature is None:
            self._signature = (self.j, self.s2[1] if FLAGS.s2 else None) + self.s1 + self.s0 + \
                              self.s0lcrc + self.s1lcrc # q1/q2 not needed
        return self._signature

    def __hash__(self):
        '''dynamic programming signature. see also __eq__().'''
        if self._hash is None:
            self._hash = hash(self.signature())  # or tuple(...)
                
        return self._hash

    def __eq__(self, other):
        '''used in dynamic programming equivalence test. ordering still uses __cmp__.''' 
        return self._signature == other._signature and \
               self.i == other.i

    def mergewith(self, other):
        '''merging equivalent states according to DP. THE CORE METHOD.'''

        if self.action == 0: # SHIFT:
            self.leftptrs.append(other.leftptrs[0]) # assert len(other.leftptrs)==1
            
        else: # REDUCE
            if FLAGS.forest or FLAGS.oracle or FLAGS.kbest >= 1:
                self.backptrs.append(other.backptrs[0])
        
    def __str__(self, top=False):
        s = "*" if self.gold else " "
        s += "{0.step}({0.rank}): {0.score:6.2f} {0.inside:6.2f} : ({0.i}, {0.j}) ".format(self)
        s += "(%d, %d) " % (len(self.leftptrs), len(self.backptrs))
        if FLAGS.debuglevel >=2:
            s += "\t%s" % self.all_actions()
        if FLAGS.debuglevel >=2 and top:
            s += "\t%s %s | %s %s %s" % (self.s1[0], self.s0[0],\
                                         self.q0[0], self.s0lcrc, self.s1lcrc)
        return s

    def inside_actions(self):

        if self.backptrs:            
            (children, action, _) = self.backptrs[0]
            this = [action] #[State.names[action]]            
            if children is None:
                return this # SHIFT
            else:
                left, right = children  # REDUCE
                return left.inside_actions() + right.inside_actions() + this
        else:
            return []

    def tree(self):
        '''alternatively: simulate actions'''

        (children, action, _) = self.backptrs[0]
        if children is None:
            return DepTree(self.i) # SHIFT
        else:
            left, right = children  # REDUCE            
            return DepTree.combine(left.tree(), right.tree(), action)

    def all_actions(self):
        s = []
        item = self
        while item:
#             if FLAGS.debuglevel >=1:
#                 print >> logs, item, s
            s = item.inside_actions() + s
            if item.leftptrs:
                # N.B.: only approximately true.
                # for incomplete derivations, this is just a guess of 1-best (as it's most likely)                
                item = item.leftptrs[0] 
            else:
                break

        return s

    def derivation_count(self, cache=None):
        ''' number of possible (inside) derivations '''

        if cache is None:
            cache = {}
        if self in cache:
            return cache[self]

        if self.action == 0:
            cache[self] = 1
        else:
            cache[self] = sum(left.derivation_count() * right.derivation_count() \
                              for ((left, right), action, _) in self.backptrs)
        return cache[self]

    def previsit(self, cache=None, cache2=None):
        ''' just count how many nodes are reachable (useful for forest) '''

        if cache is None:
            cache = set()
        if cache2 is None:
            cache2 = set()

        sig = (self.step, self.rank)
        if sig in cache:
            return 

        cache.add(sig)

        if self.action != 0:
            for ((left, right), action, _) in self.backptrs:
                left.previsit(cache, cache2)
                right.previsit(cache, cache2)

        cache2.add(sig)
        self.nodeid = len(cache2)

    def postvisit(self, cache=None):
        ''' real dumping '''

        if cache is None:
            cache = set()

        sig = (self.step, self.rank)
        if sig in cache:
            return 

        cache.add(sig)

        if self.action != 0:
            for ((left, right), action, _) in self.backptrs:
                left.postvisit(cache)
                right.postvisit(cache)

            c = len(self.backptrs)
        else:
            c = 0 # SHIFT STATE
            
        print "%d\t%d [%d-%d]\t%d ||| %s/%s=1" % (self.nodeid, self.headidx, self.i, self.j, c,\
                                                  self.s0[0], self.s0[1])
        if self.action != 0:
            for ((left, right), action, addcost) in self.backptrs:
                print "\t%d %d ||| 0=%f dir=%s" % (left.nodeid, right.nodeid, \
                                                    addcost, \
                                                    action)

    def besteval(self, reflinks, cache=None):
        ''' sub-oracle '''

        if cache is None:
            cache = {}

        sig = (self.step, self.rank)
        if sig in cache:
            return cache[sig]        
            
        if self.action == 0:
            s = DepVal()
            t = self.tree()
        else:
            h = self.headidx
            s = -1
            t = None
            for ((left, right), action, _) in self.backptrs:
                m = left.headidx if action == 1 else right.headidx
                this = 1 if (m in reflinks and reflinks[m] == h) else 0
                thistot = 1 if (m in reflinks) else 0
                
                lefteval, lefttree = left.besteval(reflinks, cache)
                righteval, righttree = right.besteval(reflinks, cache)

                thiseval = DepVal(yes=this, tot=thistot) + lefteval + righteval
                
                if thiseval > s:
                    s = thiseval
                    t = DepTree.combine(lefttree, righttree, action)
                    
        cache[sig] = s, t
        return s, t
        
    def make_feats(self, action):
        if self.step == 0:
            return []
        
        if self._feats is None:
            # use automatically generated code
            self._feats = self.model.eval_module.static_eval(self.q0, self.q1, self.q2, self.s0, self.s1, self.s2, self.s0lcrc, self.s1lcrc)

        aa = "=>" + NewState.names[action]
        return [(ff + aa) for ff in self._feats]

    def featscore(self, action):
        if self.step == 0:
            return 0 # model.new_vector()
        sig = (action, self.signature())
        NewState.tot += 1
        if sig in NewState.actionfeatscache:
            NewState.shared += 1  
            return NewState.actionfeatscache[sig]

        if FLAGS.debuglevel >= 3:
            print >> logs, zip(self.make_feats(action), \
                               ["%.2f" % self.model.weights[ff] for ff in self.make_feats(action)])
        
        score = sum([self.model.weights[ff] for ff in self.make_feats(action)])

        NewState.actionfeatscache[sig] = score
        return score
