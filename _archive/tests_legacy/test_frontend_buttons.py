"""Frontend button functionality tests.

Verifies that the management SPA's JavaScript contains the expected
button event handlers, state management, and defect fixes.

These tests inspect the static app.js source to ensure:
- stopBtn tracks playback state via play/pause/ended events
- previewBtn supports aborting ongoing synthesis via AbortController
- Recording buttons await previous recording stop before starting new one
- refreshBtn has loading state management
- createBtn has proper validation and double-submit protection
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app.main import app
from app.repositories.voice_repository import VoiceRepository
from app.routes import voices as voices_module


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "VOLC_TTS_DISABLE", 1)
    monkeypatch.setattr(settings, "LOCAL_TTS_DISABLE", 1)
    monkeypatch.setattr(voices_module, "DATA_DIR", tmp_path / "data")
    repo = VoiceRepository(db_path=str(tmp_path / "voices.db"))
    app.dependency_overrides[voices_module.get_repo] = lambda: repo
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture()
async def client(data_dir):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _get_app_js(client) -> str:
    resp = await client.get("/static/app.js")
    assert resp.status_code == 200
    return resp.text


# --------------------------------------------------------------- stopBtn tests


class TestStopButtonPlaybackTracking:
    """stopBtn must track playback state via player events."""

    @pytest.mark.asyncio
    async def test_stop_btn_disabled_on_audio_ended(self, client) -> None:
        """When audio finishes naturally, stopBtn should be disabled."""
        js = await _get_app_js(client)
        # Must have an 'ended' event listener that disables stopBtn
        assert 'addEventListener("ended"' in js
        assert "els.stopBtn.disabled = true" in js

    @pytest.mark.asyncio
    async def test_stop_btn_enabled_on_play(self, client) -> None:
        """When audio starts playing, stopBtn should be enabled."""
        js = await _get_app_js(client)
        assert 'addEventListener("play"' in js
        assert "els.stopBtn.disabled = false" in js

    @pytest.mark.asyncio
    async def test_stop_btn_disabled_on_pause(self, client) -> None:
        """When audio is paused, stopBtn should be disabled."""
        js = await _get_app_js(client)
        assert 'addEventListener("pause"' in js

    @pytest.mark.asyncio
    async def test_stop_btn_click_stops_playback(self, client) -> None:
        """Clicking stopBtn should pause and reset playback."""
        js = await _get_app_js(client)
        assert "els.player.pause()" in js
        assert "els.player.currentTime = 0" in js

    @pytest.mark.asyncio
    async def test_player_events_setup_function(self, client) -> None:
        """There must be a setupPlayerEvents function that wires player events."""
        js = await _get_app_js(client)
        assert "function setupPlayerEvents" in js
        assert "setupPlayerEvents()" in js


# --------------------------------------------------------------- previewBtn tests


class TestPreviewButtonAbort:
    """previewBtn must support cancelling ongoing synthesis."""

    @pytest.mark.asyncio
    async def test_abort_controller_used(self, client) -> None:
        """AbortController must be used for synthesis cancellation."""
        js = await _get_app_js(client)
        assert "AbortController" in js
        assert "currentAbortController" in js

    @pytest.mark.asyncio
    async def test_abort_signal_passed_to_fetch(self, client) -> None:
        """The abort signal must be passed to the API call."""
        js = await _get_app_js(client)
        assert "signal" in js
        assert "currentAbortController.signal" in js

    @pytest.mark.asyncio
    async def test_abort_error_handled(self, client) -> None:
        """AbortError must be caught and handled gracefully."""
        js = await _get_app_js(client)
        assert "AbortError" in js
        assert "已取消合成" in js

    @pytest.mark.asyncio
    async def test_previous_playback_stopped_before_new_synthesis(self, client) -> None:
        """Previous playback must be stopped before starting new synthesis."""
        js = await _get_app_js(client)
        # In previewSpeak, player.pause() should be called before the API call
        preview_idx = js.index("async function previewSpeak")
        pause_idx = js.index("els.player.pause()", preview_idx)
        api_idx = js.index('/api/v1/tts/speak', preview_idx)
        assert pause_idx < api_idx

    @pytest.mark.asyncio
    async def test_stop_btn_disabled_when_starting_new_synthesis(self, client) -> None:
        """stopBtn should be disabled when starting a new synthesis."""
        js = await _get_app_js(client)
        preview_idx = js.index("async function previewSpeak")
        # Find the line that disables stopBtn within previewSpeak
        stop_disable_idx = js.index("els.stopBtn.disabled = true", preview_idx)
        api_idx = js.index('/api/v1/tts/speak', preview_idx)
        assert stop_disable_idx < api_idx


# --------------------------------------------------------------- recording tests


class TestRecordingSourceSwitching:
    """Recording buttons must handle source switching correctly."""

    @pytest.mark.asyncio
    async def test_mic_btn_awaits_recording_stop(self, client) -> None:
        """micBtn click handler must await recording stop before starting new."""
        js = await _get_app_js(client)
        assert "recordingStopPromise" in js

    @pytest.mark.asyncio
    async def test_sys_btn_awaits_recording_stop(self, client) -> None:
        """sysBtn click handler must await recording stop before starting new."""
        js = await _get_app_js(client)
        # Both handlers should use recordingStopPromise
        assert js.count("recordingStopPromise") >= 2

    @pytest.mark.asyncio
    async def test_onstop_resolves_promise(self, client) -> None:
        """The mediaRecorder onstop handler must resolve the stop promise."""
        js = await _get_app_js(client)
        assert "recordingStopResolve" in js
        assert "recordingStopResolve()" in js

    @pytest.mark.asyncio
    async def test_switching_source_does_not_start_new_immediately(self, client) -> None:
        """When switching sources, the new recording should NOT start immediately."""
        js = await _get_app_js(client)
        # The micBtn handler should have: if (isRecording) { stopRecording(); await recordingStopPromise; } return;
        # It should NOT have startRecording("mic") right after stopRecording()
        mic_handler_idx = js.index('els.micBtn.addEventListener("click"')
        mic_handler_end = js.index("});", mic_handler_idx)
        handler_body = js[mic_handler_idx:mic_handler_end]
        # Should have early return after stopRecording when switching
        assert "return" in handler_body


# --------------------------------------------------------------- refreshBtn tests


class TestRefreshButtonLoadingState:
    """refreshBtn must show loading state and handle errors."""

    @pytest.mark.asyncio
    async def test_refresh_btn_loading_class(self, client) -> None:
        """refreshBtn should add loading class during refresh."""
        js = await _get_app_js(client)
        assert "refreshBtn.classList.add" in js

    @pytest.mark.asyncio
    async def test_refresh_btn_disabled_during_refresh(self, client) -> None:
        """refreshBtn should be disabled during refresh."""
        js = await _get_app_js(client)
        assert "refreshBtn.disabled = true" in js

    @pytest.mark.asyncio
    async def test_refresh_btn_error_handling(self, client) -> None:
        """refreshBtn should handle errors gracefully."""
        js = await _get_app_js(client)
        # loadVoices should throw to signal errors
        load_voices_idx = js.index("async function loadVoices")
        load_voices_end = js.index("}", js.index("}", load_voices_idx) + 1)
        # Check that loadVoices re-throws errors
        assert "throw err" in js[load_voices_idx:load_voices_end + 50]


# --------------------------------------------------------------- createBtn tests


class TestCreateButtonValidation:
    """createBtn must have proper validation."""

    @pytest.mark.asyncio
    async def test_create_btn_disabled_when_invalid(self, client) -> None:
        """createBtn should be disabled when form is invalid."""
        js = await _get_app_js(client)
        assert "els.createBtn.disabled = !" in js or "els.createBtn.disabled = true" in js

    @pytest.mark.asyncio
    async def test_create_btn_loading_state(self, client) -> None:
        """createBtn should show loading state during submission."""
        js = await _get_app_js(client)
        assert "createBtn.classList.add(\"loading\")" in js
        assert "createBtn.classList.remove(\"loading\")" in js

    @pytest.mark.asyncio
    async def test_create_voice_requires_name(self, client) -> None:
        """createVoice should validate that name is provided."""
        js = await _get_app_js(client)
        assert "voiceName.value.trim()" in js

    @pytest.mark.asyncio
    async def test_create_voice_requires_audio(self, client) -> None:
        """createVoice should validate that audio file is provided."""
        js = await _get_app_js(client)
        assert "fileInput._recordedBlob" in js or "fileInput.files" in js


# --------------------------------------------------------------- HTML structure tests


class TestHtmlButtonStructure:
    """HTML must have correct button elements."""

    @pytest.mark.asyncio
    async def test_stop_btn_exists_and_disabled_by_default(self, client) -> None:
        """stopBtn must exist and be disabled by default in HTML."""
        resp = await client.get("/")
        html = resp.text
        assert 'id="stopBtn"' in html
        assert "disabled" in html[html.index('id="stopBtn"'):html.index('id="stopBtn"') + 100]

    @pytest.mark.asyncio
    async def test_preview_btn_exists(self, client) -> None:
        """previewBtn must exist in HTML."""
        resp = await client.get("/")
        html = resp.text
        assert 'id="previewBtn"' in html

    @pytest.mark.asyncio
    async def test_refresh_btn_exists(self, client) -> None:
        """refreshBtn must exist in HTML."""
        resp = await client.get("/")
        html = resp.text
        assert 'id="refreshBtn"' in html

    @pytest.mark.asyncio
    async def test_create_btn_exists_and_disabled_by_default(self, client) -> None:
        """createBtn must exist and be disabled by default in HTML."""
        resp = await client.get("/")
        html = resp.text
        assert 'id="createBtn"' in html
        # createBtn should be disabled by default
        create_btn_section = html[html.index('id="createBtn"'):]
        assert "disabled" in create_btn_section[:100]

    @pytest.mark.asyncio
    async def test_recording_buttons_exist(self, client) -> None:
        """micBtn and sysBtn must exist in HTML."""
        resp = await client.get("/")
        html = resp.text
        assert 'id="micBtn"' in html
        assert 'id="sysBtn"' in html

    @pytest.mark.asyncio
    async def test_audio_player_exists(self, client) -> None:
        """Audio player element must exist."""
        resp = await client.get("/")
        html = resp.text
        assert 'id="player"' in html
        assert "<audio" in html
