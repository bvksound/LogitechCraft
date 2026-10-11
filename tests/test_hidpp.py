import pytest

from craftcrown import hidpp
from craftcrown.device import CrownService, enumerate_candidates

from .fakes import CRAFT_SLOT, CROWN_INDEX, FakeCraftTransport, fake_enumerate, wait_for


def test_build_long_pads_to_20_bytes():
    msg = hidpp.build_long(1, 0x0E, 2, b"\x02\x00")
    assert msg[:6] == bytes([0x11, 0x01, 0x0E, 0x2A, 0x02, 0x00])
    assert len(msg) == hidpp.LONG_LEN


def test_crown_event_decoding():
    ev = hidpp.CrownEvent.from_params(bytes([0, 0xFD, 0xFF, 0, 0, 0, 0]))
    assert (ev.rotation, ev.ratchet, ev.tap) == (-3, -1, False)
    ev = hidpp.CrownEvent.from_params(bytes([0, 5, 1, 0, 0, 1, 1]))
    assert ev.rotation == 5 and ev.ratchet == 1 and ev.tap and ev.press_started


def test_request_and_feature_lookup():
    t = FakeCraftTransport()
    dev = hidpp.HidppDevice(t, CRAFT_SLOT)
    assert dev.feature_index(hidpp.FEATURE_CROWN) == CROWN_INDEX
    assert dev.name() == "Craft Advanced Keyboard"


def test_error_reply_raises():
    t = FakeCraftTransport()
    dev = hidpp.HidppDevice(t, 1)  # empty receiver slot
    with pytest.raises(hidpp.HidppError):
        dev.feature_index(hidpp.FEATURE_CROWN)


def test_notifications_during_request_are_forwarded():
    t = FakeCraftTransport()
    seen = []
    dev = hidpp.HidppDevice(t, CRAFT_SLOT, on_message=seen.append)
    t.notify_crown(ratchet=1)
    dev.feature_index(hidpp.FEATURE_CROWN)
    assert len(seen) == 1 and seen[0].sub_id == CROWN_INDEX


def test_candidates_group_windows_collections():
    infos = [
        {"path": b"short", "product_id": 0xC52B, "usage_page": 0xFF00, "usage": 1,
         "interface_number": 2, "serial_number": ""},
        {"path": b"long", "product_id": 0xC52B, "usage_page": 0xFF00, "usage": 2,
         "interface_number": 2, "serial_number": ""},
        {"path": b"kbd", "product_id": 0xC52B, "usage_page": 0x0001, "usage": 6,
         "interface_number": 0, "serial_number": ""},
    ]
    (cand,) = enumerate_candidates(lambda v, p: infos)
    assert cand.paths == [b"short", b"long"] and cand.long_path == b"long" and cand.is_receiver


def test_service_diverts_and_streams_events():
    transport = FakeCraftTransport()
    events, statuses, feels = [], [], []
    svc = CrownService(events.append, lambda text, ok: statuses.append(ok),
                       on_scroll_feel=feels.append,
                       transport_factory=lambda path: transport, enumerate_fn=fake_enumerate)
    svc.start()
    try:
        assert wait_for(lambda: True in statuses)
        assert transport.mode[0] == hidpp.CROWN_DIVERT_HIDPP
        assert feels == ["ratchet"]
        transport.notify_crown(ratchet=-2)
        assert wait_for(lambda: events)
        assert events[0].ratchet == -2
    finally:
        svc.stop()
    # Crown is handed back to the OS on exit.
    assert transport.mode[0] == hidpp.CROWN_DIVERT_HID


def _split_collections():
    long_t = FakeCraftTransport(b"long")
    short_t = FakeCraftTransport(b"short")
    long_t._reply_short = lambda devnum, sub_id, address, payload: short_t.inbox.put(
        bytes([0x10, devnum, sub_id, address]) + payload.ljust(3, b"\0")[:3])
    return long_t, short_t


def test_short_error_on_other_collection_is_an_error_not_a_timeout():
    """Windows: receiver errors arrive on the short collection; probing must not wait for a timeout."""
    from craftcrown.device import CompositeTransport

    long_t, short_t = _split_collections()
    io = CompositeTransport(long_t, [short_t, long_t])
    with pytest.raises(hidpp.HidppError):
        hidpp.HidppDevice(io, 1).feature_index(hidpp.FEATURE_CROWN, timeout=5)


def test_service_with_split_windows_collections():
    long_t, short_t = _split_collections()
    infos = [
        {"path": b"short", "vendor_id": hidpp.LOGITECH_VID, "product_id": 0xC52B,
         "usage_page": 0xFF00, "usage": 1, "interface_number": 2, "serial_number": ""},
        {"path": b"long", "vendor_id": hidpp.LOGITECH_VID, "product_id": 0xC52B,
         "usage_page": 0xFF00, "usage": 2, "interface_number": 2, "serial_number": ""},
    ]
    statuses = []
    svc = CrownService(lambda ev: None, lambda text, ok: statuses.append(ok),
                       transport_factory={b"long": long_t, b"short": short_t}.get,
                       enumerate_fn=lambda v, p: infos)
    svc.start()
    try:
        assert wait_for(lambda: True in statuses)
        assert all(w[0] == 0x11 for w in long_t.writes) and not short_t.writes
    finally:
        svc.stop()


def test_service_rediverts_after_keyboard_reconnects():
    transport = FakeCraftTransport()
    statuses = []
    svc = CrownService(lambda ev: None, lambda text, ok: statuses.append(ok),
                       transport_factory=lambda path: transport, enumerate_fn=fake_enumerate)
    svc.start()
    try:
        assert wait_for(lambda: transport.mode[0] == hidpp.CROWN_DIVERT_HIDPP)
        transport.mode[0] = hidpp.CROWN_DIVERT_HID  # keyboard slept and forgot the diversion
        transport.inbox.put(bytes([0x10, CRAFT_SLOT, 0x41, 0x04, 0x00, 0x00, 0x00]))  # link up
        assert wait_for(lambda: transport.mode[0] == hidpp.CROWN_DIVERT_HIDPP, timeout=2)
    finally:
        svc.stop()
