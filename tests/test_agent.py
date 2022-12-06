import os
import tempfile
import unittest
from itertools import product
from unittest.mock import MagicMock

import casadi as cs
import numpy as np
from csnlp import MultistartNlp, Nlp, Solution, scaling
from csnlp.util import io
from csnlp.wrappers import Mpc, NlpScaling
from parameterized import parameterized, parameterized_class

from mpcrl.agents.agent import Agent

TMPFILENAME: str = ""
OPTS = {
    "expand": True,
    "print_time": False,
    "ipopt": {
        "max_iter": 500,
        "sb": "yes",
        # for debugging
        "print_level": 0,
        "print_user_options": "no",
        "print_options_documentation": "no",
    },
}


def get_dynamics(g: float, alpha: float, dt: float) -> cs.Function:
    x, u, d = cs.SX.sym("x", 3), cs.SX.sym("u", 2), cs.SX.sym("d", 3)
    x_next = x + cs.vertcat(x[1], u[0] / x[2] - g, -alpha * u[0]) * dt + d * 0
    return cs.Function("F", [x, u, d], [x_next], ["x", "u", "d"], ["x+"])


def get_mpc(horizon: int, multistart: bool):
    N = horizon
    T = 100
    K = 3
    dt = T / N
    yT = 100000
    g = 9.81
    alpha = 1 / (300 * g)
    nlp = MultistartNlp(sym_type="MX", starts=K) if multistart else Nlp(sym_type="MX")
    y_nom = 1e5
    v_nom = 2e3
    m_nom = 3e5
    u_nom = 1e8
    scaler = scaling.Scaler()
    scaler.register("y", scale=y_nom)
    scaler.register("y_0", scale=y_nom)
    scaler.register("v", scale=v_nom)
    scaler.register("v_0", scale=v_nom)
    scaler.register("m", scale=m_nom)
    scaler.register("m_0", scale=m_nom)
    scaler.register("u1", scale=u_nom)
    scaler.register("u2", scale=u_nom)
    nlp = NlpScaling(nlp, scaler=scaler, warns=False)
    mpc = Mpc(nlp, prediction_horizon=N)
    y, _ = mpc.state("y")
    _, _ = mpc.state("v")
    m, _ = mpc.state("m", lb=0)
    mpc.action("u1", lb=0, ub=5e7)
    mpc.action("u2", lb=0, ub=5e7)
    mpc.disturbance("d", 3)
    F = get_dynamics(g, alpha, dt)
    mpc.dynamics = F
    mpc.constraint("yT", y[-1], "==", yT)
    mpc.minimize(m[0] - m[-1])
    mpc.init_solver(OPTS)
    return mpc


@parameterized_class("multistart_nlp", [(True,), (False,)])
class TestAgent(unittest.TestCase):
    def tearDown(self) -> None:
        try:
            os.remove(f"{TMPFILENAME}.pkl")
        finally:
            return super().tearDown()

    def test_init__raises__mpc_with_no_actions(self):
        with self.assertRaisesRegex(
            ValueError, "Expected Mpc with na>0; got na=0 instead."
        ):
            Agent(mpc=Mpc(Nlp(), 4))

    def test_init__instantiates_V_and_Q_as_two_different_mpcs(self):
        agent = Agent(mpc=get_mpc(3, self.multistart_nlp))
        self.assertIsInstance(agent.Q, Mpc)
        self.assertIsInstance(agent.V, Mpc)
        self.assertIsNot(agent.Q, agent.V)

    def test_init__instantiates_V_and_Q_correctly(self):
        agent = Agent(mpc=get_mpc(3, self.multistart_nlp))
        self.assertIn(agent.cost_perturbation_parameter, agent.V.parameters.keys())
        self.assertNotIn(agent.cost_perturbation_parameter, agent.Q.parameters.keys())
        self.assertIn(agent.init_action_parameter, agent.Q.parameters.keys())
        self.assertIn(agent.init_action_constraint, agent.Q.constraints.keys())
        self.assertNotIn(agent.init_action_parameter, agent.V.parameters.keys())
        self.assertNotIn(agent.init_action_constraint, agent.V.constraints.keys())

    def test_unwrapped(self):
        agent = Agent(mpc=get_mpc(3, self.multistart_nlp))
        agent2 = agent.unwrapped
        self.assertIs(agent, agent2)

    def test_copy(self):  # sourcery skip: class-extract-method
        agent1 = Agent(mpc=get_mpc(3, self.multistart_nlp))
        agent2 = agent1.copy()
        self.assertIsNot(agent1, agent2)
        self.assertIsNot(agent1.Q, agent2.Q)
        self.assertIsNot(agent1.V, agent2.V)

    def test__is_pickleable(self):
        agent1 = Agent(mpc=get_mpc(3, self.multistart_nlp))

        global TMPFILENAME
        TMPFILENAME = next(tempfile._get_candidate_names())
        with agent1.pickleable():
            io.save(TMPFILENAME, agent=agent1, check=42)

        loadeddata = io.load(TMPFILENAME)
        self.assertEqual(loadeddata["check"], 42)
        agent2: Agent = loadeddata["agent"]
        self.assertIsInstance(agent2, Agent)
        self.assertIsInstance(agent2.Q, Mpc)
        self.assertIsInstance(agent2.V, Mpc)
        self.assertIsNot(agent1, agent2)
        self.assertIsNot(agent1.Q, agent2.Q)
        self.assertIsNot(agent1.V, agent2.V)

    @parameterized.expand(product(["V", "Q"], [False, True], [False, True]))
    def test_solve_mpc__calls_mpc_with_correct_args(
        self, mpctype: str, vector: bool, multiple_pars: bool
    ):
        if not self.multistart_nlp:
            multiple_pars = False
        mpc = get_mpc(3, self.multistart_nlp)
        agent = Agent(mpc=mpc)
        vals0 = object()
        sol = Solution(
            f=5, vars=None, vals=vals0, stats={"success": True}, _get_value=None
        )
        agent._last_solution = sol
        mpc: Mpc[cs.SX] = getattr(agent, mpctype)
        if self.multistart_nlp:
            mpc.nlp.solve_multi = MagicMock(return_value=sol)
        else:
            mpc.nlp.solve = MagicMock(return_value=sol)

        s = {"y": 0, "v": 10, "m": 5e5}
        a = {"u1": 1, "u2": 2}
        if vector:
            s = cs.DM(s.values())
            a = cs.DM(a.values())
        if mpctype == "V":
            a = None
        pars = {agent.cost_perturbation_parameter: [42, 69], "d": cs.DM([5, 6, 7])}
        pars_ = (
            (pars.copy() for _ in range(mpc.nlp.starts))
            if multiple_pars
            else pars.copy()
        )

        agent.solve_mpc(mpc, state=s, action=a, pars=pars_)

        call_pars = {
            **pars,
            "y_0": s[0] if vector else s["y"],
            "v_0": s[1] if vector else s["v"],
            "m_0": s[2] if vector else s["m"],
        }
        if mpctype != "V":
            call_pars[Agent.init_action_parameter] = a if vector else cs.DM(a.values())
        if self.multistart_nlp:
            mpc.nlp.solve_multi.assert_called_once()
            kwargs = mpc.nlp.solve_multi.call_args.kwargs
        else:
            mpc.nlp.solve.assert_called_once()
            kwargs = mpc.nlp.solve.call_args.kwargs
        self.assertIs(kwargs["vals0"], vals0)
        if multiple_pars:
            for pars_i in kwargs["pars"]:
                self.assertEqual(len(mpc.unwrapped._pars.keys() - pars_i.keys()), 0)
                for key in call_pars:
                    np.testing.assert_allclose(pars_i[key], call_pars[key], rtol=0)
        else:
            pars = kwargs["pars"]
            self.assertEqual(len(mpc.unwrapped._pars.keys() - pars.keys()), 0)
            for key in call_pars:
                np.testing.assert_allclose(pars[key], call_pars[key], rtol=0)



if __name__ == "__main__":
    unittest.main()
