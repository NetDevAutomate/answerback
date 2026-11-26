"""Measured machine capacity and placement advice; no routing or work movement.

`probe` and `assess` measure and judge; `facts` reads what a person recorded about each
machine in the policy file; `choose` is a pure choice of a machine on those three.
`rebalance` is a pure plan of at most one move of a worker, and `rebalance_cli` previews it;
neither applies a move.
"""
