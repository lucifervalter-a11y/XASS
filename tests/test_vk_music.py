"""Tests for VK music import helpers (no network)."""
from __future__ import annotations

from app.services.vk_audio_url_decoder import decode_audio_url, VkAudioUrlDecodeError
from app.services.vk_music import VkTrack, decode_vk_url


def test_decode_vk_url_passthrough_plain():
    url = "https://cs1.vkuseraudio.net/p1/abc.mp3"
    assert decode_vk_url(url, 1) == url


def test_decode_vk_url_empty():
    assert decode_vk_url("", 1) == ""


def test_decode_vk_url_unavailable_raises_safe():
    bad = "https://m.vk.com/mp3/audio_api_unavailable.mp3?extra=ZZZ#YYY"
    assert decode_vk_url(bad, 1) == ""


def test_vk_track_full_id():
    t = VkTrack(owner_id=1, audio_id=2, access_key="abc")
    assert t.full_id == "1_2_abc"
    t2 = VkTrack(owner_id=1, audio_id=2)
    assert t2.full_id == "1_2"


def test_vk_track_to_dict():
    t = VkTrack(owner_id=9, audio_id=8, artist="A", title="B", url="http://x")
    d = t.to_dict()
    assert d["owner_id"] == 9 and d["has_url"] is True and d["source"] == "vk"
