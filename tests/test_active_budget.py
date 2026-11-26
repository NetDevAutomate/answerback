import asyncio

import pytest

from answerback_bridge.active_budget import ActiveBudget


def test_nested_waits_charge_only_active_time():
    now = [10.0]
    budget = ActiveBudget(30, clock=lambda: now[0])
    now[0] += 5
    budget.pause()
    now[0] += 100
    budget.pause()
    now[0] += 100
    budget.resume()
    assert budget.remaining == 25
    now[0] += 100
    budget.resume()
    now[0] += 7
    assert budget.remaining == 18


async def test_expiration_cancels_owned_operation():
    finished = asyncio.Event()

    async def operation():
        try:
            await asyncio.Future()
        finally:
            finished.set()

    with pytest.raises(TimeoutError):
        await ActiveBudget(0.01).run(operation())
    assert finished.is_set()


async def test_permission_wait_can_outlast_budget():
    budget = ActiveBudget(0.02)

    async def operation():
        budget.pause()
        await asyncio.sleep(0.04)
        budget.resume()
        return "done"

    assert await budget.run(operation()) == "done"


async def test_caller_cancel_during_wait_leaves_no_operation():
    entered = asyncio.Event()
    finished = asyncio.Event()
    budget = ActiveBudget(5)

    async def operation():
        try:
            budget.pause()
            entered.set()
            await asyncio.Future()
        finally:
            budget.resume()
            finished.set()

    task = asyncio.create_task(budget.run(operation()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


# T-50 fix round 1 (B2): two read-only facts that a plugin's worker run asks when a
# TimeoutError reaches it. Red at b680c8d (neither attribute exists there).


def test_budget_reports_the_seconds_it_was_made_with():
    assert ActiveBudget(600).seconds == 600
    assert ActiveBudget(0.35).seconds == 0.35


async def test_budget_is_not_expired_after_a_body_that_ended_normally():
    budget = ActiveBudget(5)
    assert budget.expired is False
    async with budget.limit():
        await asyncio.sleep(0)
    assert budget.expired is False


async def test_budget_is_expired_after_its_limit_expired():
    budget = ActiveBudget(0.01)
    with pytest.raises(TimeoutError):
        async with budget.limit():
            await asyncio.Future()
    assert budget.expired is True


async def test_budget_is_not_expired_when_the_body_raised_its_own_timeout_error():
    budget = ActiveBudget(5)
    with pytest.raises(TimeoutError):
        async with budget.limit():
            raise TimeoutError("an operation of the body timed out")
    assert budget.expired is False


@pytest.mark.parametrize("expires", [False, True])
async def test_budget_expiry_fact_survives_a_pause_and_a_resume(expires):
    budget = ActiveBudget(0.05)

    async def body() -> None:
        budget.pause()
        await asyncio.sleep(0.1)  # a human decision: the budget does not run
        budget.resume()
        if expires:
            await asyncio.Future()

    if expires:
        with pytest.raises(TimeoutError):
            await budget.run(body())
    else:
        await budget.run(body())
    assert budget.expired is expires
