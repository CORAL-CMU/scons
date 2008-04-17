#
# __COPYRIGHT__
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
# KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#

__doc__ = """
Generic Taskmaster module for the SCons build engine.

This module contains the primary interface(s) between a wrapping user
interface and the SCons build engine.  There are two key classes here:

    Taskmaster
        This is the main engine for walking the dependency graph and
        calling things to decide what does or doesn't need to be built.

    Task
        This is the base class for allowing a wrapping interface to
        decide what does or doesn't actually need to be done.  The
        intention is for a wrapping interface to subclass this as
        appropriate for different types of behavior it may need.

        The canonical example is the SCons native Python interface,
        which has Task subclasses that handle its specific behavior,
        like printing "`foo' is up to date" when a top-level target
        doesn't need to be built, and handling the -c option by removing
        targets as its "build" action.  There is also a separate subclass
        for suppressing this output when the -q option is used.

        The Taskmaster instantiates a Task object for each (set of)
        target(s) that it decides need to be evaluated and/or built.
"""

__revision__ = "__FILE__ __REVISION__ __DATE__ __DEVELOPER__"

import SCons.compat

from itertools import chain
import operator
import string
import sys
import traceback

import SCons.Errors
import SCons.Node

StateString = SCons.Node.StateString
NODE_NO_STATE = SCons.Node.no_state
NODE_PENDING = SCons.Node.pending
NODE_EXECUTING = SCons.Node.executing
NODE_UP_TO_DATE = SCons.Node.up_to_date
NODE_EXECUTED = SCons.Node.executed
NODE_FAILED = SCons.Node.failed


# A subsystem for recording stats about how different Nodes are handled by
# the main Taskmaster loop.  There's no external control here (no need for
# a --debug= option); enable it by changing the value of CollectStats.

CollectStats = None

class Stats:
    """
    A simple class for holding statistics about the disposition of a
    Node by the Taskmaster.  If we're collecting statistics, each Node
    processed by the Taskmaster gets one of these attached, in which case
    the Taskmaster records its decision each time it processes the Node.
    (Ideally, that's just once per Node.)
    """
    def __init__(self):
        """
        Instantiates a Taskmaster.Stats object, initializing all
        appropriate counters to zero.
        """
        self.considered  = 0
        self.already_handled  = 0
        self.problem  = 0
        self.child_failed  = 0
        self.not_built  = 0
        self.side_effects  = 0
        self.build  = 0

StatsNodes = []

fmt = "%(considered)3d "\
      "%(already_handled)3d " \
      "%(problem)3d " \
      "%(child_failed)3d " \
      "%(not_built)3d " \
      "%(side_effects)3d " \
      "%(build)3d "

def dump_stats():
    StatsNodes.sort(lambda a, b: cmp(str(a), str(b)))
    for n in StatsNodes:
        print (fmt % n.stats.__dict__) + str(n)



class Task:
    """
    Default SCons build engine task.

    This controls the interaction of the actual building of node
    and the rest of the engine.

    This is expected to handle all of the normally-customizable
    aspects of controlling a build, so any given application
    *should* be able to do what it wants by sub-classing this
    class and overriding methods as appropriate.  If an application
    needs to customze something by sub-classing Taskmaster (or
    some other build engine class), we should first try to migrate
    that functionality into this class.

    Note that it's generally a good idea for sub-classes to call
    these methods explicitly to update state, etc., rather than
    roll their own interaction with Taskmaster from scratch.
    """
    def __init__(self, tm, targets, top, node):
        self.tm = tm
        self.targets = targets
        self.top = top
        self.node = node
        self.exc_clear()

    def display(self, message):
        """
        Hook to allow the calling interface to display a message.

        This hook gets called as part of preparing a task for execution
        (that is, a Node to be built).  As part of figuring out what Node
        should be built next, the actually target list may be altered,
        along with a message describing the alteration.  The calling
        interface can subclass Task and provide a concrete implementation
        of this method to see those messages.
        """
        pass

    def prepare(self):
        """
        Called just before the task is executed.

        This is mainly intended to give the target Nodes a chance to
        unlink underlying files and make all necessary directories before
        the Action is actually called to build the targets.
        """

        # Now that it's the appropriate time, give the TaskMaster a
        # chance to raise any exceptions it encountered while preparing
        # this task.
        self.exception_raise()

        if self.tm.message:
            self.display(self.tm.message)
            self.tm.message = None

        # Let the targets take care of any necessary preparations.
        # This includes verifying that all of the necessary sources
        # and dependencies exist, removing the target file(s), etc.
        #
        # As of April 2008, the get_executor().prepare() method makes
        # sure that all of the aggregate sources necessary to build this
        # Task's target(s) exist in one up-front check.  The individual
        # target t.prepare() methods check that each target's explicit
        # or implicit dependencies exists, and also initialize the
        # .sconsign info.
        self.targets[0].get_executor().prepare()
        for t in self.targets:
            t.prepare()
            for s in t.side_effects:
                s.prepare()

    def get_target(self):
        """Fetch the target being built or updated by this task.
        """
        return self.node

    def needs_execute(self):
        """
        Called to determine whether the task's execute() method should
        be run.

        This method allows one to skip the somethat costly execution
        of the execute() method in a seperate thread. For example,
        that would be unnecessary for up-to-date targets.
        """
        return True

    def execute(self):
        """
        Called to execute the task.

        This method is called from multiple threads in a parallel build,
        so only do thread safe stuff here.  Do thread unsafe stuff in
        prepare(), executed() or failed().
        """

        try:
            everything_was_cached = 1
            for t in self.targets:
                if not t.retrieve_from_cache():
                    everything_was_cached = 0
                    break
            if not everything_was_cached:
                self.targets[0].build()
        except SystemExit:
            exc_value = sys.exc_info()[1]
            raise SCons.Errors.ExplicitExit(self.targets[0], exc_value.code)
        except SCons.Errors.UserError:
            raise
        except SCons.Errors.BuildError:
            raise
        except:
            raise SCons.Errors.TaskmasterException(self.targets[0],
                                                   sys.exc_info())

    def executed_without_callbacks(self):
        """
        Called when the task has been successfully executed
        and the Taskmaster instance doesn't want to call
        the Node's callback methods.
        """
        for t in self.targets:
            if t.get_state() == NODE_EXECUTING:
                for side_effect in t.side_effects:
                    side_effect.set_state(NODE_NO_STATE)
                t.set_state(NODE_EXECUTED)

    def executed_with_callbacks(self):
        """
        Called when the task has been successfully executed and
        the Taskmaster instance wants to call the Node's callback
        methods.

        This may have been a do-nothing operation (to preserve build
        order), so we must check the node's state before deciding whether
        it was "built", in which case we call the appropriate Node method.
        In any event, we always call "visited()", which will handle any
        post-visit actions that must take place regardless of whether
        or not the target was an actual built target or a source Node.
        """
        for t in self.targets:
            if t.get_state() == NODE_EXECUTING:
                for side_effect in t.side_effects:
                    side_effect.set_state(NODE_NO_STATE)
                t.set_state(NODE_EXECUTED)
                t.built()
            t.visited()

    executed = executed_with_callbacks

    def failed(self):
        """
        Default action when a task fails:  stop the build.
        """
        self.fail_stop()

    def fail_stop(self):
        """
        Explicit stop-the-build failure.
        """
        
        # Invoke fail_continue() to clean-up the pending children
        # list.
        self.fail_continue()

        # Tell the taskmaster to not start any new tasks
        self.tm.stop()

        # We're stopping because of a build failure, but give the
        # calling Task class a chance to postprocess() the top-level
        # target under which the build failure occurred.
        self.targets = [self.tm.current_top]
        self.top = 1

    def fail_continue(self):
        """
        Explicit continue-the-build failure.

        This sets failure status on the target nodes and all of
        their dependent parent nodes.
        """
        
        pending_children = self.tm.pending_children

        to_visit = set()
        for t in self.targets:
            # Set failure state on all of the parents that were dependent
            # on this failed build.
            if t.state != NODE_FAILED:
                t.state = NODE_FAILED
                parents = t.waiting_parents
                to_visit = to_visit | parents
                pending_children = pending_children - parents

        try:
            while 1:
                try:
                    node = to_visit.pop()
                except AttributeError:
                    # Python 1.5.2
                    if len(to_visit):
                        node = to_visit[0]
                        to_visit.remove(node)
                    else:
                        break
                if node.state != NODE_FAILED:
                    node.state = NODE_FAILED
                    parents = node.waiting_parents
                    to_visit = to_visit | parents
                    pending_children = pending_children - parents
        except KeyError:
            # The container to_visit has been emptied.
            pass

        # We have the stick back the pending_children list into the
        # task master because the python 1.5.2 compatibility does not
        # allow us to use in-place updates
        self.tm.pending_children = pending_children

    def make_ready_all(self):
        """
        Marks all targets in a task ready for execution.

        This is used when the interface needs every target Node to be
        visited--the canonical example being the "scons -c" option.
        """
        self.out_of_date = self.targets[:]
        for t in self.targets:
            t.disambiguate().set_state(NODE_EXECUTING)
            for s in t.side_effects:
                s.set_state(NODE_EXECUTING)

    def make_ready_current(self):
        """
        Marks all targets in a task ready for execution if any target
        is not current.

        This is the default behavior for building only what's necessary.
        """
        self.out_of_date = []
        needs_executing = False
        for t in self.targets:
            try:
                t.disambiguate().make_ready()
                is_up_to_date = not t.has_builder() or \
                                (not t.always_build and t.is_up_to_date())
            except EnvironmentError, e:
                raise SCons.Errors.BuildError(node=t, errstr=e.strerror, filename=e.filename)

            if not is_up_to_date:
                self.out_of_date.append(t)
                needs_executing = True

        if needs_executing:
            for t in self.targets:
                t.set_state(NODE_EXECUTING)
                for s in t.side_effects:
                    s.set_state(NODE_EXECUTING)
        else:                
            for t in self.targets:
                # We must invoke visited() to ensure that the node
                # information has been computed before allowing the
                # parent nodes to execute. (That could occur in a
                # parallel build...)
                t.visited()
                t.set_state(NODE_UP_TO_DATE)

    make_ready = make_ready_current

    def postprocess(self):
        """
        Post-processes a task after it's been executed.

        This examines all the targets just built (or not, we don't care
        if the build was successful, or even if there was no build
        because everything was up-to-date) to see if they have any
        waiting parent Nodes, or Nodes waiting on a common side effect,
        that can be put back on the candidates list.
        """

        # We may have built multiple targets, some of which may have
        # common parents waiting for this build.  Count up how many
        # targets each parent was waiting for so we can subtract the
        # values later, and so we *don't* put waiting side-effect Nodes
        # back on the candidates list if the Node is also a waiting
        # parent.

        targets = set(self.targets)

        parents = {}
        for t in targets:
            for p in t.waiting_parents:
                parents[p] = parents.get(p, 0) + 1

        for t in targets:
            for s in t.side_effects:
                if s.get_state() == NODE_EXECUTING:
                    s.set_state(NODE_NO_STATE)
                    for p in s.waiting_parents:
                        parents[p] = parents.get(p, 0) + 1
                for p in s.waiting_s_e:
                    if p.ref_count == 0:
                        self.tm.candidates.append(p)
                        self.tm.pending_children.discard(p)

        for p, subtract in parents.items():
            p.ref_count = p.ref_count - subtract
            if p.ref_count == 0:
                self.tm.candidates.append(p)
                self.tm.pending_children.discard(p)

        for t in targets:
            t.postprocess()

    # Exception handling subsystem.
    #
    # Exceptions that occur while walking the DAG or examining Nodes
    # must be raised, but must be raised at an appropriate time and in
    # a controlled manner so we can, if necessary, recover gracefully,
    # possibly write out signature information for Nodes we've updated,
    # etc.  This is done by having the Taskmaster tell us about the
    # exception, and letting

    def exc_info(self):
        """
        Returns info about a recorded exception.
        """
        return self.exception

    def exc_clear(self):
        """
        Clears any recorded exception.

        This also changes the "exception_raise" attribute to point
        to the appropriate do-nothing method.
        """
        self.exception = (None, None, None)
        self.exception_raise = self._no_exception_to_raise

    def exception_set(self, exception=None):
        """
        Records an exception to be raised at the appropriate time.

        This also changes the "exception_raise" attribute to point
        to the method that will, in fact
        """
        if not exception:
            exception = sys.exc_info()
        self.exception = exception
        self.exception_raise = self._exception_raise

    def _no_exception_to_raise(self):
        pass

    def _exception_raise(self):
        """
        Raises a pending exception that was recorded while getting a
        Task ready for execution.
        """
        exc = self.exc_info()[:]
        try:
            exc_type, exc_value, exc_traceback = exc
        except ValueError:
            exc_type, exc_value = exc
            exc_traceback = None
        raise exc_type, exc_value, exc_traceback


def find_cycle(stack, visited):
    if stack[-1] in visited:
        return None
    visited.add(stack[-1])
    for n in stack[-1].waiting_parents:
        stack.append(n)
        if stack[0] == stack[-1]:
            return stack
        if find_cycle(stack, visited):
            return stack
        stack.pop()
    return None


class Taskmaster:
    """
    The Taskmaster for walking the dependency DAG.
    """

    def __init__(self, targets=[], tasker=Task, order=None, trace=None):
        self.original_top = targets
        self.top_targets_left = targets[:]
        self.top_targets_left.reverse()
        self.candidates = []
        self.tasker = tasker
        if not order:
            order = lambda l: l
        self.order = order
        self.message = None
        self.trace = trace
        self.next_candidate = self.find_next_candidate
        self.pending_children = set()


    def find_next_candidate(self):
        """
        Returns the next candidate Node for (potential) evaluation.

        The candidate list (really a stack) initially consists of all of
        the top-level (command line) targets provided when the Taskmaster
        was initialized.  While we walk the DAG, visiting Nodes, all the
        children that haven't finished processing get pushed on to the
        candidate list.  Each child can then be popped and examined in
        turn for whether *their* children are all up-to-date, in which
        case a Task will be created for their actual evaluation and
        potential building.

        Here is where we also allow candidate Nodes to alter the list of
        Nodes that should be examined.  This is used, for example, when
        invoking SCons in a source directory.  A source directory Node can
        return its corresponding build directory Node, essentially saying,
        "Hey, you really need to build this thing over here instead."
        """
        try:
            return self.candidates.pop()
        except IndexError:
            pass
        try:
            node = self.top_targets_left.pop()
        except IndexError:
            return None
        self.current_top = node
        alt, message = node.alter_targets()
        if alt:
            self.message = message
            self.candidates.append(node)
            self.candidates.extend(self.order(alt))
            node = self.candidates.pop()
        return node

    def no_next_candidate(self):
        """
        Stops Taskmaster processing by not returning a next candidate.
        """
        return None

    def _find_next_ready_node(self):
        """
        Finds the next node that is ready to be built.

        This is *the* main guts of the DAG walk.  We loop through the
        list of candidates, looking for something that has no un-built
        children (i.e., that is a leaf Node or has dependencies that are
        all leaf Nodes or up-to-date).  Candidate Nodes are re-scanned
        (both the target Node itself and its sources, which are always
        scanned in the context of a given target) to discover implicit
        dependencies.  A Node that must wait for some children to be
        built will be put back on the candidates list after the children
        have finished building.  A Node that has been put back on the
        candidates list in this way may have itself (or its sources)
        re-scanned, in order to handle generated header files (e.g.) and
        the implicit dependencies therein.

        Note that this method does not do any signature calculation or
        up-to-date check itself.  All of that is handled by the Task
        class.  This is purely concerned with the dependency graph walk.
        """

        self.ready_exc = None

        T = self.trace
        if T: T.write('\nTaskmaster: Looking for a node to evaluate\n')

        while 1:
            node = self.next_candidate()
            if node is None:
                if T: T.write('Taskmaster: No candidate anymore.\n\n')
                return None

            node = node.disambiguate()
            state = node.get_state()

            if CollectStats:
                if not hasattr(node, 'stats'):
                    node.stats = Stats()
                    StatsNodes.append(node)
                S = node.stats
                S.considered = S.considered + 1
            else:
                S = None
  
            if T: T.write('Taskmaster:     Considering node <%-10s %s> and its children:\n' % 
                          (StateString[node.get_state()], repr(str(node))))

            if state == NODE_NO_STATE:
                # Mark this node as being on the execution stack:
                node.set_state(NODE_PENDING)
            elif state > NODE_PENDING:
                # Skip this node if it has already been evaluated:
                if S: S.already_handled = S.already_handled + 1
                if T: T.write('Taskmaster:        already handled (executed)\n')
                continue

            try:
                children = node.children()
            except SystemExit:
                exc_value = sys.exc_info()[1]
                e = SCons.Errors.ExplicitExit(node, exc_value.code)
                self.ready_exc = (SCons.Errors.ExplicitExit, e)
                if T: T.write('Taskmaster:        SystemExit\n')
                return node
            except:
                # We had a problem just trying to figure out the
                # children (like a child couldn't be linked in to a
                # VariantDir, or a Scanner threw something).  Arrange to
                # raise the exception when the Task is "executed."
                self.ready_exc = sys.exc_info()
                if S: S.problem = S.problem + 1
                if T: T.write('Taskmaster:        exception while scanning children.\n')
                return node

            children_not_visited = []
            children_pending = set()
            children_not_ready = []
            children_failed = False

            for child in chain(children,node.prerequisites):
                childstate = child.get_state()

                if T: T.write('Taskmaster:        <%-10s %s>\n' % 
                              (StateString[childstate], repr(str(child))))

                if childstate == NODE_NO_STATE:
                    children_not_visited.append(child)
                elif childstate == NODE_PENDING:
                    children_pending.add(child)
                elif childstate == NODE_FAILED:
                    children_failed = True

                if childstate <= NODE_EXECUTING:
                    children_not_ready.append(child)


            # These nodes have not even been visited yet.  Add
            # them to the list so that on some next pass we can
            # take a stab at evaluating them (or their children).
            children_not_visited.reverse()
            self.candidates.extend(self.order(children_not_visited))

            # Skip this node if any of its children have failed.
            #
            # This catches the case where we're descending a top-level
            # target and one of our children failed while trying to be
            # built by a *previous* descent of an earlier top-level
            # target.
            #
            # It can also occur if a node is reused in multiple
            # targets. One first descends though the one of the
            # target, the next time occurs through the other target.
            #
            # Note that we can only have failed_children if the
            # --keep-going flag was used, because without it the build
            # will stop before diving in the other branch.
            #
            # Note that even if one of the children fails, we still
            # added the other children to the list of candidate nodes
            # to keep on building (--keep-going).
            if children_failed:
                node.set_state(NODE_FAILED)

                if S: S.child_failed = S.child_failed + 1
                if T: T.write('Taskmaster:****** <%-10s %s>\n' % 
                              (StateString[node.get_state()], repr(str(node))))
                continue

            if children_not_ready:
                for child in children_not_ready:
                    # We're waiting on one or more derived targets
                    # that have not yet finished building.
                    if S: S.not_built = S.not_built + 1

                    # Add this node to the waiting parents lists of
                    # anything we're waiting on, with a reference
                    # count so we can be put back on the list for
                    # re-evaluation when they've all finished.
                    node.ref_count =  node.ref_count + child.add_to_waiting_parents(node)

                self.pending_children = self.pending_children | children_pending
                
                continue

            # Skip this node if it has side-effects that are
            # currently being built:
            wait_side_effects = False
            for se in node.side_effects:
                if se.get_state() == NODE_EXECUTING:
                    se.add_to_waiting_s_e(node)
                    wait_side_effects = True

            if wait_side_effects:
                if S: S.side_effects = S.side_effects + 1
                continue

            # The default when we've gotten through all of the checks above:
            # this node is ready to be built.
            if S: S.build = S.build + 1
            if T: T.write('Taskmaster: Evaluating <%-10s %s>\n' % 
                          (StateString[node.get_state()], repr(str(node))))
            return node

        return None

    def next_task(self):
        """
        Returns the next task to be executed.

        This simply asks for the next Node to be evaluated, and then wraps
        it in the specific Task subclass with which we were initialized.
        """
        node = self._find_next_ready_node()

        if node is None:
            return None

        tlist = node.get_executor().targets

        task = self.tasker(self, tlist, node in self.original_top, node)
        try:
            task.make_ready()
        except:
            # We had a problem just trying to get this task ready (like
            # a child couldn't be linked in to a VariantDir when deciding
            # whether this node is current).  Arrange to raise the
            # exception when the Task is "executed."
            self.ready_exc = sys.exc_info()

        if self.ready_exc:
            task.exception_set(self.ready_exc)

        self.ready_exc = None

        return task

    def stop(self):
        """
        Stops the current build completely.
        """
        self.next_candidate = self.no_next_candidate

    def cleanup(self):
        """
        Check for dependency cycles.
        """
        if self.pending_children:
            desc = 'Found dependency cycle(s):\n'
            for node in self.pending_children:
                cycle = find_cycle([node], set())
                if cycle:
                    desc = desc + "  " + string.join(map(str, cycle), " -> ") + "\n"
                else:
                    desc = desc + "  Internal Error: no cycle found for node %s (%s)\n" %  \
                        (node, repr(node)) 
            raise SCons.Errors.UserError, desc
