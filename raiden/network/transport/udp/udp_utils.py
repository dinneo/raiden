import random

import gevent
from gevent.event import (
    _AbstractLinkable,
    Event,
)
from raiden.utils.gevent_utils import RaidenGreenletEvent
from raiden.utils import typing
# type alias to avoid both circular dependencies and flake8 errors
UDPTransport = 'UDPTransport'


def event_first_of(*events: _AbstractLinkable) -> Event:
    """ Waits until one of `events` is set.

    The event returned is /not/ cleared with any of the `events`, this value
    must not be reused if the clearing behavior is used.
    """
    first_finished = RaidenGreenletEvent()

    if not all(isinstance(e, _AbstractLinkable) for e in events):
        raise ValueError('all events must be linkable')

    for event in events:
        event.rawlink_safe(lambda _: first_finished.set())

    return first_finished


def timeout_exponential_backoff(
        retries: int,
        timeout: int,
        maximum: int,
) -> typing.Generator[int, None, None]:
    """ Timeouts generator with an exponential backoff strategy.

    Timeouts start spaced by `timeout`, after `retries` exponentially increase
    the retry delays until `maximum`, then maximum is returned indefinitely.
    """
    yield timeout

    tries = 1
    while tries < retries:
        tries += 1
        yield timeout

    while timeout < maximum:
        timeout = min(timeout * 2, maximum)
        yield timeout

    while True:
        yield maximum


def timeout_two_stage(
        retries: int,
        timeout1: int,
        timeout2: int,
) -> int:
    """ Timeouts generator with a two stage strategy

    Timeouts start spaced by `timeout1`, after `retries` increase
    to `timeout2` which is repeated indefinitely.
    """
    for _ in range(retries):
        yield timeout1
    while True:
        yield timeout2


def retry(
        transport: UDPTransport,
        messagedata: bytes,
        message_id: typing.MessageID,
        recipient: typing.Address,
        event_stop: Event,
        timeout_backoff: typing.Generator[int, None, None],
) -> bool:
    """ Send messagedata until it's acknowledged.

    Exit when:

    - The message is delivered.
    - Event_stop is set.
    - The iterator timeout_backoff runs out.

    Returns:
        bool: True if the message was acknowledged, False otherwise.
    """

    async_result = transport.maybe_sendraw_with_result(
        recipient,
        messagedata,
        message_id,
    )

    event_quit = event_first_of(
        async_result,
        event_stop,
    )

    for timeout in timeout_backoff:

        if event_quit.wait(timeout=timeout) is True:
            break

        transport.maybe_sendraw_with_result(
            recipient,
            messagedata,
            message_id,
        )

    return async_result.ready()


def wait_recovery(event_stop: Event, event_healthy: Event):
    event_first_of(
        event_stop,
        event_healthy,
    ).wait()

    if event_stop.is_set():
        return

    # There may be multiple threads waiting, do not restart them all at
    # once to avoid message flood.
    gevent.sleep(random.random())


def retry_with_recovery(
        transport: UDPTransport,
        messagedata: bytes,
        message_id: typing.MessageID,
        recipient: typing.Address,
        event_stop: Event,
        event_healthy: Event,
        event_unhealthy: Event,
        backoff: typing.Generator[int, None, None],
) -> bool:
    """ Send messagedata while the node is healthy until it's acknowledged.

    Note:
        backoff must be an infinite iterator, otherwise this task will
        become a hot loop.
    """

    # The underlying unhealthy will be cleared, care must be taken to properly
    # clear stop_or_unhealthy too.
    stop_or_unhealthy = event_first_of(
        event_stop,
        event_unhealthy,
    )

    acknowledged = False
    while not event_stop.is_set() and not acknowledged:

        # Packets must not be sent to an unhealthy node, nor should the task
        # wait for it to become available if the message has been acknowledged.
        if event_unhealthy.is_set():
            wait_recovery(
                event_stop,
                event_healthy,
            )

            # Assume wait_recovery returned because unhealthy was cleared and
            # continue execution, this is safe to do because event_stop is
            # checked below.
            stop_or_unhealthy.clear()

            if event_stop.is_set():
                return acknowledged

        acknowledged = retry(
            transport,
            messagedata,
            message_id,
            recipient,

            # retry will stop when this event is set, allowing this task to
            # wait for recovery when the node becomes unhealthy or to quit if
            # the stop event is set.
            stop_or_unhealthy,

            # Intentionally reusing backoff to restart from the last
            # timeout/number of iterations.
            backoff,
        )

    return acknowledged
