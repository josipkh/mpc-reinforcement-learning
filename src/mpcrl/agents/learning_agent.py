from abc import ABC, abstractmethod
from functools import wraps
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    Generic,
    Iterable,
    Literal,
    Optional,
    TypeVar,
    Union,
)

import numpy as np
import numpy.typing as npt
from csnlp.wrappers import Mpc

from mpcrl.agents.agent import ActType, Agent, ObsType, SymType, _update_dicts
from mpcrl.core.callbacks import LearningAgentCallbacks
from mpcrl.core.experience import ExperienceReplay
from mpcrl.core.exploration import ExplorationStrategy, NoExploration
from mpcrl.core.parameters import LearnableParametersDict
from mpcrl.core.update import UpdateStrategy
from mpcrl.util.random import generate_seeds
from mpcrl.util.types import GymEnvLike

ExpType = TypeVar("ExpType")


class LearningAgent(
    Agent[SymType], LearningAgentCallbacks, ABC, Generic[SymType, ExpType]
):
    """Base class for a learning agent with MPC as policy provider where the main method
    `update`, which is called to update the learnable parameters of the MPC according to
    the underlying learning methodology (e.g., Bayesian Optimization, RL, etc.) is
    abstract and must be implemented by inheriting classes.

    Note: this class makes no assumptions on the learning methodology used to update the
    MPC's learnable parameters."""

    __slots__ = ("_experience", "_learnable_pars", "_update_strategy", "_raises")

    def __init__(
        self,
        mpc: Mpc[SymType],
        update_strategy: Union[int, UpdateStrategy],
        learnable_parameters: LearnableParametersDict[SymType],
        fixed_parameters: Union[
            None, Dict[str, npt.ArrayLike], Collection[Dict[str, npt.ArrayLike]]
        ] = None,
        exploration: Optional[ExplorationStrategy] = None,
        experience: Optional[ExperienceReplay[ExpType]] = None,
        warmstart: Literal["last", "last-successful"] = "last-successful",
        name: Optional[str] = None,
    ) -> None:
        """Instantiates the learning agent.

        Parameters
        ----------
        mpc : Mpc[casadi.SX or MX]
            The MPC controller used as policy provider by this agent. The instance is
            modified in place to create the approximations of the state function `V(s)`
            and action value function `Q(s,a)`, so it is recommended not to modify it
            further after initialization of the agent. Moreover, some parameter and
            constraint names will need to be created, so an error is thrown if these
            names are already in use in the mpc. These names are under the attributes
            `perturbation_parameter`, `action_parameter` and `action_constraint`.
        update_strategy : UpdateStrategy or int
            The strategy used to decide which frequency to update the mpc parameters
            with. If an `int` is passed, then the default strategy that updates every
            `n` env's steps is used (where `n` is the argument passed); otherwise, an
            instance of `UpdateStrategy` can be passed to specify these in more details.
        learnable_parameters : LearnableParametersDict
            A special dict containing the learnable parameters of the MPC, together with
            their bounds and values. This dict is complementary with `fixed_parameters`,
            which contains the MPC parameters that are not learnt by the agent.
        fixed_parameters : dict[str, array_like] or collection of, optional
            A dict (or collection of dict, in case of `csnlp.MultistartNlp`) whose keys
            are the names of the MPC parameters and the values are their corresponding
            values. Use this to specify fixed parameters, that is, non-learnable. If
            `None`, then no fixed parameter is assumed.
        exploration : ExplorationStrategy, optional
            Exploration strategy for inducing exploration in the MPC policy. By default
            `None`, in which case `NoExploration` is used in the fixed-MPC agent.
        experience : ExperienceReplay, optional
            The container for experience replay memory. If `None` is passed, then a
            memory with length 1 is created, i.e., it keeps only the latest memory
            transition.
        warmstart: 'last' or 'last-successful', optional
            The warmstart strategy for the MPC's NLP. If 'last-successful', the last
            successful solution is used to warm start the solver for the next iteration.
            If 'last', the last solution is used, regardless of success or failure.
        name : str, optional
            Name of the agent. If `None`, one is automatically created from a counter of
            the class' instancies.
        """
        Agent.__init__(
            self,
            mpc=mpc,
            fixed_parameters=fixed_parameters,
            warmstart=warmstart,
            name=name,
        )
        LearningAgentCallbacks.__init__(self)

        # save to fields
        self._raises: bool = True
        self._learnable_pars = learnable_parameters
        self._experience = (
            ExperienceReplay(maxlen=1) if experience is None else experience
        )

        # if an exploration is passed, save it and hook it up
        if exploration is not None:
            self._exploration = exploration
            if not isinstance(self._exploration, NoExploration):
                self._hook_callbacks(exploration.hook, exploration.step)

        # save the update strategy and hook it up
        if not isinstance(update_strategy, UpdateStrategy):
            update_strategy = UpdateStrategy(update_strategy)
        self._update_strategy = update_strategy
        assert update_strategy.hook in {
            "on_episode_end",
            "on_env_step",
        }, "Updates can be hooked only to episode_end or env_step."
        args_idx, kwargs_keys = (
            (1, ("episode",))
            if update_strategy.hook == "on_episode_end"
            else (slice(1, 3), ("episode", "timestep"))
        )
        self._hook_callbacks(
            update_strategy.hook,
            self._check_and_perform_update,
            args_idx,  # type: ignore[arg-type]
            kwargs_keys,
        )

    @property
    def experience(self) -> ExperienceReplay[ExpType]:
        """Gets the experience replay memory of the agent."""
        return self._experience

    @property
    def update_strategy(self) -> UpdateStrategy:
        """Gets the update strategy of the agent."""
        return self._update_strategy

    @property
    def learnable_parameters(self) -> LearnableParametersDict[SymType]:
        """Gets the parameters of the MPC that can be learnt by the agent."""
        return self._learnable_pars

    def store_experience(self, item: ExpType) -> None:
        """Stores the given item in the agent's memory for later experience replay.

        Parameters
        ----------
        item : experience-type
            Item to be stored in memory.
        """
        self._experience.append(item)

    @staticmethod
    def train(
        agent: "LearningAgent[SymType, ExpType]",
        env: GymEnvLike[ObsType, ActType],
        episodes: int,
        seed: Union[None, int, Iterable[int]] = None,
        raises: bool = True,
        env_reset_options: Optional[Dict[str, Any]] = None,
    ) -> npt.NDArray[np.double]:
        """Train the agent on an environment.

        Parameters
        ----------
        agent : LearningAgent or inheriting
            The agent to train.
        env : GymEnvLike[ObsType, ActType]
            A gym-like environment where to train the agent in.
        episodes : int
            Number of training episodes.
        seed : int or iterable of ints, optional
            Each env's seed for RNG.
        raises : bool, optional
            If `True`, when any of the MPC solver runs fails, or when an update fails,
            the corresponding error is raised; otherwise, only a warning is raised.
        env_reset_options : dict, optional
            Additional information to specify how the environment is reset at each
            training episode (optional, depending on the specific environment).

        Returns
        -------
        array of doubles
            The cumulative returns for each training episode.

        Raises
        ------
        MpcSolverError or MpcSolverWarning
            Raises the error or the warning (depending on `raises`) if any of the MPC
            solvers fail.
        UpdateError or UpdateWarning
            Raises the error or the warning (depending on `raises`) if the update fails.
        """
        # prepare for training start
        agent._raises = raises
        returns = np.zeros(episodes, dtype=float)
        agent.on_training_start(env)

        for episode, current_seed in zip(range(episodes), generate_seeds(seed)):
            agent.on_episode_start(env, episode)
            agent.reset()
            state, _ = env.reset(seed=current_seed, options=env_reset_options)
            returns[episode] = agent.train_one_episode(
                agent=agent,
                env=env,
                episode=episode,
                init_state=state,
                raises=raises,
            )
            agent.on_episode_end(env, episode, returns[episode])

        agent.on_training_end(env, returns)
        return returns

    @staticmethod
    @abstractmethod
    def train_one_episode(
        agent: "LearningAgent[SymType, ExpType]",
        env: GymEnvLike[ObsType, ActType],
        episode: int,
        init_state: ObsType,
        raises: bool = True,
    ) -> float:
        """Train the agent on an environment for one episode.

        Parameters
        ----------
        agent : LearningAgent or inheriting
            The agent to train.
        env : GymEnvLike[ObsType, ActType]
            A gym-like environment where to train the agent in.
        episode : int
            Number of the current training episode.
        init_state : observation type
            Initial state/observation of the environment.
        raises : bool, optional
            If `True`, when any of the MPC solver runs fails, or when an update fails,
            the corresponding error is raised; otherwise, only a warning is raised.

        Returns
        -------
        float
            The cumulative rewards for this training episode.

        Raises
        ------
        MpcSolverError or MpcSolverWarning
            Raises the error or the warning (depending on `raises`) if any of the MPC
            solvers fail.
        UpdateError or UpdateWarning
            Raises the error or the warning (depending on `raises`) if the update fails.
        """

    @abstractmethod
    def update(self) -> Optional[str]:
        """Updates the learnable parameters of the MPC according to the agent's learning
        algorithm.

        Returns
        -------
        errormsg : str or None
            In case the update fails, an error message is returned to be raised as error
            or warning; otherwise, `None` is returned.
        """

    def _hook_callbacks(
        self,
        callbackname: str,
        to_call: Callable,
        args_idx: Union[None, int, slice] = None,
        kwargs_keys: Optional[Collection[str]] = None,
    ) -> None:
        """Internal decorator to hook, e.g., exploration decay, learning rate decay and
        update strategy to the various callbacks."""

        if args_idx is None:
            args_idx = slice(0, 0)
        if kwargs_keys is None:
            kwargs_keys = tuple()

        def decorate(method: Callable) -> Callable:
            @wraps(method)
            def wrapper(*args, **kwargs):
                out = method(*args, **kwargs)
                to_call(
                    *args[args_idx],
                    **{k: kwargs[k] for k in kwargs_keys if k in kwargs},
                )
                return out

            return wrapper

        setattr(self, callbackname, decorate(getattr(self, callbackname)))

    def _check_and_perform_update(self, episode: int, timestep: Optional[int]) -> None:
        """Internal utility to check if an update is due and perform it."""
        if not self._update_strategy.can_update():
            return
        update_msg = self.update()
        if update_msg is not None:
            self.on_update_failure(episode, timestep, update_msg, self._raises)
        self.on_update()

    def _get_parameters(
        self,
    ) -> Union[None, Dict[str, npt.ArrayLike], Collection[Dict[str, npt.ArrayLike]]]:
        """Internal utility to retrieve parameters of the MPC in order to solve it.
        `LearningAgent` returns both fixed and learnable parameters."""
        learnable_pars = self._learnable_pars.value_as_dict
        fixed_pars = self._fixed_pars
        if fixed_pars is None:
            return learnable_pars  # type: ignore[return-value]
        if isinstance(fixed_pars, dict):
            fixed_pars.update(learnable_pars)
            return fixed_pars
        return tuple(
            _update_dicts(self._fixed_pars, learnable_pars)  # type: ignore[arg-type]
        )
