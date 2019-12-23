import weakref

from abc import ABC, abstractmethod
from collections import UserDict
from collections.abc import Mapping

from unification import unify, reify, Var
from unification.core import _reify

from .util import FlexibleSet


class ConstraintStore(ABC):
    """A class that enforces constraints between logic variables in a miniKanren state.

    Attributes
    ----------
    lvar_constraints: MutableMapping
        A mapping of logic variables to sets of objects that define their
        constraints (e.g. a set of items with which the logic variable cannot
        be unified).  The mapping's values are entirely determined by the
        ConstraintStore implementation.

    """

    __slots__ = ("lvar_constraints", "op_str")

    def __init__(self, op_str, lvar_constraints=None):
        self.op_str = op_str
        # self.lvar_constraints = weakref.WeakKeyDictionary(lvar_constraints)
        self.lvar_constraints = lvar_constraints or dict()

    @abstractmethod
    def pre_unify_check(self, lvar_map, lvar=None, value=None):
        """Check a key-value pair before they're added to a ConstrainedState."""
        raise NotImplementedError()

    @abstractmethod
    def post_unify_check(self, lvar_map, lvar=None, value=None, old_state=None):
        """Check a key-value pair after they're added to a ConstrainedState."""
        raise NotImplementedError()

    def add(self, lvar, lvar_constraint, **kwargs):
        """Add a new constraint."""
        if lvar not in self.lvar_constraints:
            self.lvar_constraints[lvar] = FlexibleSet([lvar_constraint])
        else:
            self.lvar_constraints[lvar].add(lvar_constraint)

    def constraints_str(self, lvar):
        """Print the constraints on a logic variable."""
        if lvar in self.lvar_constraints:
            return f"{self.op_str} {self.lvar_constraints[lvar]}"
        else:
            return ""

    def __contains__(self, lvar):
        return lvar in self.lvar_constraints

    def __eq__(self, other):
        return (
            type(self) == type(other)
            and self.op_str == other.op_str
            and self.lvar_constraints == other.lvar_constraints
        )

    def __repr__(self):
        return f"ConstraintStore({self.op_str}: {self.lvar_constraints})"


class ConstrainedState(UserDict):
    """A miniKanren state that holds unifications of logic variables and upholds constraints on logic variables."""

    __slots__ = ("constraints",)

    def __init__(self, *s, constraints=None):
        super().__init__(*s)
        self.constraints = dict(constraints or [])

    def pre_unify_checks(self, lvar, value):
        return all(
            cstore.pre_unify_check(self.data, lvar, value)
            for cstore in self.constraints.values()
        )

    def post_unify_checks(self, lvar_map, lvar, value):
        return all(
            cstore.post_unify_check(lvar_map, lvar, value, old_state=self)
            for cstore in self.constraints.values()
        )

    def __eq__(self, other):
        if isinstance(other, ConstrainedState):
            return self.data == other.data and self.constraints == other.constraints

        if isinstance(other, Mapping) and not self.constraints:
            return self.data == other

        return False

    def __repr__(self):
        return f"ConstrainedState({repr(self.data)}, {self.constraints})"


def unify_ConstrainedState(u, v, S):
    if S.pre_unify_checks(u, v):
        s = unify(u, v, S.data)
        if s is not False and S.post_unify_checks(s, u, v):
            return ConstrainedState(s, constraints=S.constraints)

    return False


unify.add((object, object, ConstrainedState), unify_ConstrainedState)


class ConstrainedVar(Var):
    """A logic variable that tracks its own constraints.

    Currently, this is only for display/reification purposes.

    """

    __slots__ = ("S", "var")

    def __init__(self, var, S):
        self.S = weakref.ref(S)
        self.token = var.token
        self.var = weakref.ref(var)

    def __repr__(self):
        S = self.S()
        var = self.var()
        res = super().__repr__()
        if S is not None and var is not None:
            u_constraints = ",".join(
                [c.constraints_str(var) for c in S.constraints.values()]
            )
            return f"{res}: {{{u_constraints}}}"
        else:
            return res

    def __eq__(self, other):
        if type(other) == type(self):
            return self.S == other.S and self.token == other.token
        elif type(other) == Var:
            # NOTE: A more valid comparison is same token and no constraints.
            return self.token == other.token
        return NotImplemented

    def __hash__(self):
        return hash((Var, self.token))


def reify_ConstrainedState(u, S):
    u_res = reify(u, S.data)
    return ConstrainedVar(u_res, S)


_reify.add((Var, ConstrainedState), reify_ConstrainedState)


class DisequalityStore(ConstraintStore):
    """A disequality constraint (i.e. two things do not unify)."""

    def __init__(self, lvar_constraints=None):
        super().__init__("=/=", lvar_constraints)

    def post_unify_check(self, lvar_map, lvar=None, value=None, old_state=None):

        for lv_key, constraints in list(self.lvar_constraints.items()):
            lv = reify(lv_key, lvar_map)
            constraints_rf = reify(tuple(constraints), lvar_map)

            for cs in constraints_rf:
                s = unify(lv, cs, {})

                if s is not False and not s:
                    # They already unify, but with no unground logic variables,
                    # so we have an immediate violation of the constraint.
                    return False
                elif s is False:
                    # They don't unify and have no unground logic variables, so
                    # the constraint is immediately satisfied and there's no
                    # reason to continue checking this constraint.
                    constraints.discard(cs)
                else:
                    # They unify when/if the unifications in `s` are made, so
                    # let's add these as new constraints.
                    for k, v in s.items():
                        self.add(k, v)

            if len(constraints) == 0:
                # This logic variable has no more unground constraints, so
                # remove it.
                del self.lvar_constraints[lv_key]

        return True

    def pre_unify_check(self, lvar_map, lvar=None, value=None):
        return True


def neq(u, v):
    """Construct a disequality goal."""

    def neq_goal(S):
        nonlocal u, v

        u, v = reify((u, v), S)

        # Get the unground logic variables that would unify the two objects;
        # these are all the logic variables that we can't let unify.
        s_uv = unify(u, v, {})

        if s_uv is False:
            # They don't unify and have no unground logic variables, so the
            # constraint is immediately satisfied.
            yield S
            return
        elif not s_uv:
            # They already unify, but with no unground logic variables, so we
            # have an immediate violation of the constraint.
            return

        if not isinstance(S, ConstrainedState):
            S = ConstrainedState(S)

        cs = S.constraints.setdefault(DisequalityStore, DisequalityStore())

        for lvar, obj in s_uv.items():
            cs.add(lvar, obj)

        # We need to check the current state for validity.
        if cs.post_unify_check(S.data):
            yield S

    return neq_goal
