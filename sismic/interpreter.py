from collections import deque
from itertools import combinations
from . import model
from .evaluator import Evaluator, PythonEvaluator


class MicroStep:
    """
    Create a micro step. A step consider ``event``, takes ``transition`` and results in a list
    of ``entered_states`` and a list of ``exited_states``.
    Order in the two lists is REALLY important!

    :param event: Event or None in case of eventless transition
    :param transition: a ''Transition`` or None if no processed transition
    :param entered_states: possibly empty list of entered states
    :param exited_states: possibly empty list of exited states
    """
    def __init__(self, event: model.Event=None, transition: model.Transition=None,
                 entered_states: list=None, exited_states: list=None):
        self.event = event
        self.transition = transition if transition else []
        self.entered_states = entered_states if entered_states else []
        self.exited_states = exited_states if exited_states else []

    def __repr__(self):
        return 'MicroStep({}, {}, {}, {})'.format(self.event, self.transition, self.entered_states, self.exited_states)


class MacroStep:
    """
    A macro step is a list of micro steps instances, corresponding to the process of at most one transition and
    the conseuctive stabilization micro steps.

    :param steps: a list of ``MicroStep`` instances
    """
    def __init__(self, steps: list):
        self.steps = steps

    @property
    def event(self) -> model.Event:
        """
        Event (or ``None``) that were consumed.
        """
        try:
            self.steps[0].event
        except IndexError:
            return None

    @property
    def transitions(self) -> list:
        """
        A (possibly empty) list of transitions that were triggered.
        """
        return [step.transition for step in self.steps if step.transition]

    @property
    def entered_states(self) -> list:
        """
        List of the states names that were entered.
        """
        states = []
        for step in self.steps:
            states += step.entered_states
        return states

    @property
    def exited_states(self) -> list:
        """
        List of the states names that were exited.
        """
        states = []
        for step in self.steps:
            states += step.exited_states
        return states

    def __repr__(self):
        return 'MacroStep({}, {}, {}, {})'.format(self.event, self.transitions, self.entered_states, self.exited_states)


class Interpreter:
    """
    A discrete interpreter that executes a statechart according to a semantic close to SCXML.

    :param statechart: statechart to interpret
    :param evaluator_klass: An optional callable (eg. a class) that takes no input and return a
        ``evaluator.Evaluator`` instance that will be used to initialize the interpreter.
        By default, an ``evaluator.PythonEvaluator`` will be used.
    """
    def __init__(self, statechart: model.StateChart, evaluator_klass=None):
        self._evaluator_klass = evaluator_klass
        self._evaluator = evaluator_klass() if evaluator_klass else PythonEvaluator()
        self._statechart = statechart
        self._memory = {}  # History states memory
        self._configuration = set()  # Set of active states
        self._events = deque()  # Events queue
        self._start()

    @property
    def configuration(self) -> list:
        """
        List of active states names, ordered by depth.
        """
        return sorted(self._configuration, key=lambda s: self._statechart.depth_of(s))

    @property
    def evaluator(self) -> Evaluator:
        """
        The ``Evaluator`` associated with this simulator.
        """
        return self._evaluator

    def send(self, event: model.Event, internal: bool=False):
        """
        Send an event to the interpreter, and add it into the event queue.

        :param event: an ``Event`` instance
        :param internal: set to True if the provided ``Event`` should be considered as
            an internal event (and thus, as to be prepended to the events queue).
        :return: ``self``
        """
        if internal:
            self._events.appendleft(event)
        else:
            self._events.append(event)
        return self

    def _start(self) -> list:
        """
        Make this statechart runnable:

         - Execute statechart initial code
         - Execute until a stable situation is reached.

        :return: A (possibly empty) list of executed MicroStep.
        """
        if self._statechart.on_entry:
            self._evaluator.execute_action(self._statechart.on_entry)

        # Initial step and stabilization
        step = MicroStep(entered_states=[self._statechart.initial])
        self._execute_step(step)

        return [step] + self._stabilize()

    @property
    def running(self) -> bool:
        """
        Boolean indicating whether this interpreter is not in a final configuration.
        """
        for state in self._statechart.leaf_for(list(self._configuration)):
            if not isinstance(self._statechart._states[state], model.FinalState):
                return True
        return False

    def reset(self):
        """
        Reset current interpreter to its initial state.
        This also resets history states memory.
        """
        self.__init__(self._statechart, self._evaluator_klass)

    def execute(self, max_steps: int=-1) -> list:
        """
        Repeatedly calls ``execute_once()`` and return a list containing
        the returned values of ``execute_once()``.

        Notice that this does NOT return an iterator but computes the whole list first
        before returning it.

        :param max_steps: An upper bound on the number steps that are computed and returned.
            Default is -1, no limit. Set to a positive integer to avoid infinite loops
            in the statechart execution.
        :return: A list of ``MacroStep`` instances
        """
        returned_steps = []
        i = 0
        macro_step = self.execute_once()
        while macro_step:
            returned_steps.append(macro_step)
            i += 1
            if max_steps > 0 and i == max_steps:
                break
            macro_step = self.execute_once()
        return returned_steps

    def execute_once(self) -> MacroStep:
        """
        Processes a transition based on the oldest queued event (or no event if an eventless transition
        can be processed), and stabilizes the interpreter in a stable situation (ie. processes initial states,
        history states, etc.).

        :return: a macro step or ``None`` if nothing happened
        """

        # Eventless transitions first
        event = None
        transitions = self._select_eventless_transitions()

        if len(transitions) == 0:
            # Consumes event if any
            if len(self._events) > 0:
                event = self._events.popleft()  # consumes event
                transitions = self._select_transitions(event)
                # If the event can not be processed, discard it
                if len(transitions) == 0:
                    return MacroStep([MicroStep(event=event)])
            else:
                return None  # No step to do!

        transitions = self._sort_transitions(transitions)

        # Compute and execute the steps for the transitions
        steps = self._compute_transitions_steps(event, transitions)
        for step in steps:
            self._execute_step(step)

        # Compute and execute the stabilization steps
        stabilization_steps = self._stabilize()

        return MacroStep(steps + stabilization_steps)

    def _select_eventless_transitions(self) -> list:
        """
        Return a list of eventless transitions that can be triggered.

        :return: a list of ``Transition`` instances
        """
        return self._select_transitions(None)

    def _select_transitions(self, event: model.Event) -> list:
        """
        Return a list of transitions that can be triggered according to the given event.

        :param event: event to consider
        :return: a list of ``Transition`` instances
        """
        transitions = []

        for leaf in self._statechart.leaf_for(self._configuration):
            for leaf_ancestor in [leaf] + self._statechart.ancestors_for(leaf):
                transition_found = False
                for transition in getattr(self._statechart.states[leaf_ancestor], 'transitions', []):
                    if transition.event != event or transition.from_state not in self._configuration:
                        continue
                    if transition.guard is None or self._evaluator.evaluate_condition(transition.guard, event):
                        transition_found = True
                        # Prevent duplicate
                        if transition not in transitions:
                            transitions.append(transition)

                # Do not consider current's parent if we already have a transition!
                if transition_found:
                    break

        return transitions

    def _sort_transitions(self, transitions: list) -> list:
        """
        Given a list of triggered transitions, return a list of transitions in an order that represents
        the order in which they have to be processed.

        :param transitions: a list of ``Transition`` instances
        :return: an ordered list of ``Transition`` instances
        :raise Warning: In case of non-determinism or conflicting transitions.
        """
        if len(transitions) > 1:
            # If more than one transition, we check (1) they are from separate regions and (2) they do not conflict
            # Two transitions conflict if one of them leaves the parallel state
            for t1, t2 in combinations(transitions, 2):
                # Check (1)
                lca = self._statechart.least_common_ancestor(t1.from_state, t2.from_state)
                lca_state = self._statechart.states.get(lca, None)

                # Their LCA must be an orthogonal state!
                if not isinstance(lca_state, model.OrthogonalState):
                    raise Warning('Non-determinist transitions: {t1} and {t2}' +
                                  '\nConfiguration is {c}\nEvent is {e}\nTransitions are:{t}\n'
                                  .format(c=self.configuration, e=t1.event, t=transitions, t1=t1, t2=t2))

                # Check (2)
                # This check must be done wrt. to LCA, as the combination of from_states could
                # come from nested parallel regions!
                for t in [t1, t2]:
                    last_before_lca = t.from_state
                    for state in self._statechart.ancestors_for(t.from_state):
                        if state == lca:
                            break
                        last_before_lca = state
                    # Target must be a descendant (or self) of this state
                    if t.to_state not in [last_before_lca] + self._statechart.descendants_for(last_before_lca):
                        raise Warning('Conflicting transitions: {t1} and {t2}' +
                                      '\nConfiguration is {c}\nEvent is {e}\nTransitions are:{t}\n'
                                      .format(c=self.configuration, e=t1.event, t=transitions, t1=t1, t2=t2))

            # Define an arbitrary order based on the depth and the name of source states.
            # Sort is **stable** in Python. We should sort by depth desc, then by name asc.
            # We do it first by name desc, then by depth asc, then we reverse the list.
            transitions = sorted(transitions, key=lambda t: t.from_state, reverse=True)
            transitions = sorted(transitions, key=lambda t: self._statechart.depth_of(t.from_state))
            transitions.reverse()

        return transitions

    def _compute_transitions_steps(self, event: model.Event, transitions: list) -> list:
        """
        Return a (possibly empty) list of micro steps. Each micro step corresponds to the process of a transition
        matching given event.

        :param event: the event to consider, if any
        :param transitions: the transitions that should be processed
        :return: a list of micro steps.
        """
        returned_steps = []
        for transition in transitions:
            # Internal transition
            if transition.to_state is None:
                returned_steps.append(MicroStep(event, transition, [], []))
                continue

            lca = self._statechart.least_common_ancestor(transition.from_state, transition.to_state)
            from_ancestors = self._statechart.ancestors_for(transition.from_state)
            to_ancestors = self._statechart.ancestors_for(transition.to_state)

            # Exited states
            exited_states = []

            # last_before_lca is the "highest" ancestor or from_state that is a child of LCA
            last_before_lca = transition.from_state
            for state in from_ancestors:
                if state == lca:
                    break
                last_before_lca = state

            # Take all the descendants of this state and list the ones that are active
            for descendant in self._statechart.descendants_for(last_before_lca)[::-1]:  # Mind the reversed order!
                # Only leave states that are currently active
                if descendant in self._configuration:
                    exited_states.append(descendant)

            # Add last_before_lca as it is a child of LCA that must be exited
            if last_before_lca in self._configuration:
                exited_states.append(last_before_lca)

            # Entered states
            entered_states = [transition.to_state]
            for state in to_ancestors:
                if state == lca:
                    break
                entered_states.insert(0, state)

            returned_steps.append(MicroStep(event, transition, entered_states, exited_states))

        return returned_steps

    def _compute_stabilization_step(self) -> MicroStep:
        """
        Return a stabilization step, ie. a step that lead to a more stable situation
        for the current statechart (expand to initial state, expand to history state, etc.).

        :return: A ``MicroStep`` instance or ``None`` if this statechart can not be more stabilized
        """
        # Check if we are in a set of "stable" states
        leaves = self._statechart.leaf_for(list(self._configuration))
        for leaf in leaves:
            leaf = self._statechart.states[leaf]
            if isinstance(leaf, model.HistoryState):
                states_to_enter = self._memory.get(leaf.name, [leaf.initial])
                states_to_enter.sort(key=lambda x: self._statechart.depth_of(x))
                return MicroStep(entered_states=states_to_enter, exited_states=[leaf.name])
            elif isinstance(leaf, model.OrthogonalState):
                return MicroStep(entered_states=leaf.children)
            elif isinstance(leaf, model.CompoundState) and leaf.initial:
                return MicroStep(entered_states=[leaf.initial])

    def _stabilize(self) -> list:
        """
        Compute, apply and return stabilization steps.

        :return: A list of ``MicroStep`` instances
        """
        # Stabilization
        steps = []
        step = self._compute_stabilization_step()
        while step:
            steps.append(step)
            self._execute_step(step)
            step = self._compute_stabilization_step()
        return steps

    def _execute_step(self, step: MicroStep):
        """
        Apply given ``MicroStep`` on this statechart

        :param step: ``MicroStep`` instance
        """
        entered_states = list(map(lambda s: self._statechart.states[s], step.entered_states))
        exited_states = list(map(lambda s: self._statechart.states[s], step.exited_states))

        for state in exited_states:
            # Execute exit action
            if isinstance(state, model.ActionStateMixin) and state.on_exit:
                for event in self._evaluator.execute_action(state.on_exit):
                    # Internal event
                    self.send(event, internal=True)

        # Deal with history: this only concerns compound states
        exited_compound_states = list(filter(lambda s: isinstance(s, model.CompoundState), exited_states))
        for state in exited_compound_states:
            # Look for an HistoryState among its children
            for child_name in state.children:
                child = self._statechart.states[child_name]
                if isinstance(child, model.HistoryState):
                    if child.deep:
                        # This MUST contain at least one element!
                        active = self._configuration.intersection(self._statechart.descendants_for(state.name))
                        assert len(active) >= 1
                        self._memory[child.name] = list(active)
                    else:
                        # This MUST contain exactly one element!
                        active = self._configuration.intersection(state.children)
                        assert len(active) == 1
                        self._memory[child.name] = list(active)

        # Remove states from configuration
        self._configuration = self._configuration.difference(step.exited_states)

        # Execute transition
        if step.transition and step.transition.action:
            self._evaluator.execute_action(step.transition.action, step.event)

        for state in entered_states:
            # Execute entry action
            if isinstance(state, model.ActionStateMixin) and state.on_entry:
                for event in self._evaluator.execute_action(state.on_entry):
                    # Internal event
                    self.send(event, internal=True)

        # Add state to configuration
        self._configuration = self._configuration.union(step.entered_states)

    def __repr__(self):
        return '{}[{}]'.format(self.__class__.__name__, ', '.join(self.configuration))

