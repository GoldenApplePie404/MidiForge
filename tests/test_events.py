from core.events import EventBus


def test_subscribe_publish():
    bus = EventBus()
    got = []
    bus.subscribe("midi.message", got.append)
    bus.publish("midi.message", 42)
    assert got == [42]


def test_multiple_subscribers():
    bus = EventBus()
    a, b = [], []
    bus.subscribe("t", a.append)
    bus.subscribe("t", b.append)
    bus.publish("t", 1)
    assert a == [1] and b == [1]


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    got = []
    sid = bus.subscribe("t", got.append)
    bus.publish("t", 1)
    bus.unsubscribe(sid)
    bus.publish("t", 2)
    assert got == [1]


def test_different_topic_isolated():
    bus = EventBus()
    got = []
    bus.subscribe("a", got.append)
    bus.publish("b", 1)
    assert got == []


def test_publish_without_subscribers_safe():
    EventBus().publish("nobody", 1)


def test_clear():
    bus = EventBus()
    got = []
    bus.subscribe("t", got.append)
    bus.clear()
    bus.publish("t", 1)
    assert got == []