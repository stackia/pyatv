"""RAOP functional tests with fake device.

TODO: Things to improve:

* Add tests for timing server
* Improve sync tests
* Volume changed by other protocol (multi-protocol test)
"""
import asyncio
import io
import logging
import math
from typing import Dict, List

import pytest
import pytest_asyncio

from pyatv import connect, exceptions
from pyatv.const import DeviceState, FeatureName, FeatureState, MediaType, Protocol
from pyatv.exceptions import AuthenticationError
from pyatv.interface import FeatureInfo, MediaMetadata, Playing, PushListener
from pyatv.protocols.airplay.utils import dbfs_to_pct

from tests.utils import data_path, stub_sleep, until

pytestmark = pytest.mark.asyncio

_LOGGER = logging.getLogger(__name__)

# Used by all sample audio files for now
CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes

# Number of frames per audio packet in RAOP
FRAMES_PER_PACKET = 352

ONE_FRAME_IN_BYTES = FRAMES_PER_PACKET * CHANNELS * SAMPLE_WIDTH

METADATA_FIELDS = [FeatureName.Title, FeatureName.Artist, FeatureName.Album]
PROGRESS_FIELDS = [FeatureName.Position, FeatureName.TotalTime]
VOLUME_FIELDS = [
    FeatureName.SetVolume,
    FeatureName.Volume,
    FeatureName.VolumeUp,
    FeatureName.VolumeDown,
]
REMOTE_CONTROL_FIELDS = [FeatureName.Stop, FeatureName.Pause]


@pytest_asyncio.fixture(name="playing_listener")
async def playing_listener_fixture(raop_client):
    class PlayingListener(PushListener):
        def __init__(self):
            """Initialize a new PlayingListener instance."""
            self.updates: List[Playing] = []
            self.all_features: Dict[FeatureName, FeatureInfo] = {}
            self.playing_event = asyncio.Event()

        def playstatus_update(self, updater, playstatus: Playing) -> None:
            """Inform about changes to what is currently playing."""
            self.updates.append(playstatus)
            if playstatus.device_state == DeviceState.Playing:
                self.all_features = raop_client.features.all_features()
                self.playing_event.set()

        def playstatus_error(self, updater, exception: Exception) -> None:
            """Inform about an error when updating play status."""

    listener = PlayingListener()
    raop_client.push_updater.listener = listener
    raop_client.push_updater.start()
    yield listener


async def audio_matches(
    audio: bytes,
    frames: int,
    channels: int = CHANNELS,
    sample_width: int = SAMPLE_WIDTH,
    skip_frames: int = 0,
) -> None:
    """Assert that raw audio matches audio generated by audiogen.py."""
    succeeded = True
    frame_size = channels * sample_width

    # Wait until there's enough data in the audio buffer for expected number of frames
    await until(lambda: len(audio) >= frames * channels * sample_width)

    # assert per individual frame
    for i in range(frames):
        actual = audio[i * frame_size : i * frame_size + frame_size]
        expected = frame_size * bytes([(i + skip_frames) & 0xFF])
        if actual != expected:
            _LOGGER.error("%s != %s for frame %d", actual, expected, (i + skip_frames))
            succeeded = False

    return succeeded


def assert_features_in_state(
    all_features: Dict[FeatureName, FeatureInfo],
    features: List[FeatureName],
    state: FeatureState,
) -> None:
    for feature in features:
        assert all_features[feature].state == state


@pytest.mark.parametrize(
    "raop_properties,metadata",
    [
        # Metadata supported by receiver ("md=0")
        (
            {"et": "0", "md": "0"},
            {"artist": "postlund", "album": "raop", "title": "pyatv"},
        ),
        # Metadata NOT supported by receiver
        (
            {"et": "0"},
            {"artist": None, "album": None, "title": None},
        ),
    ],
)
async def test_stream_file_verify_metadata(raop_client, raop_state, metadata):
    await raop_client.stream.stream_file(data_path("only_metadata.wav"))
    assert raop_state.metadata.artist == metadata["artist"]
    assert raop_state.metadata.album == metadata["album"]
    assert raop_state.metadata.title == metadata["title"]


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_stream_complete_file(raop_client, raop_state):
    await raop_client.stream.stream_file(data_path("audio_10_frames.wav"))

    assert await audio_matches(raop_state.raw_audio, frames=10)


@pytest.mark.skip(reason="unstable, must investigate")
@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_stream_complete_file_verify_padding(raop_client, raop_state):
    await raop_client.stream.stream_file(data_path("audio_10_frames.wav"))

    # The last packet is padded with zeroes to fill a complete packet. This means that
    # (FRAME_PER_PACKET - 10) = 342 empty frames are added to the first packet. After
    # that, empty packets are sent corresponding to the length of the latency, i.e.
    # (sample_rate + 22050) / FRAMES_PER_PACKET which gives 187.9 packets with
    # sample_rate=44100, channels=2, sample_width=2. Latency is a bit unfortunate
    # chosen to not divide evenly, but should be rounded up to 188.

    # Based on content in input file
    sample_rate = 44100
    frame_size = CHANNELS * SAMPLE_WIDTH

    # Skip initial audio frames and just extract padding
    padding = raop_state.raw_audio[frame_size * 10 :]

    # Number of frames used for padding in last audio packet (i.e. first packet here)
    padding_frames_in_audio_packet = FRAMES_PER_PACKET - 10

    # Number of frames for pure padding
    hardcoded_additional_latency = 22050
    latency_packets = math.ceil(
        (sample_rate + hardcoded_additional_latency) / FRAMES_PER_PACKET
    )
    latency_frames = latency_packets * FRAMES_PER_PACKET

    # Calculate total number of latency frames and convert that to number of bytes
    total_latency_frames = padding_frames_in_audio_packet + latency_frames
    total_latency_size_in_bytes = total_latency_frames * frame_size

    assert len(padding) == total_latency_size_in_bytes

    # Avoid allocating a large buffer to compare with
    assert padding.replace(b"\x00", b"") == b""


@pytest.mark.parametrize(
    "raop_properties,require_auth",
    [({"et": "4"}, False), ({"et": "4", "am": "AirPort10,115"}, True)],
)
async def test_stream_complete_legacy_auth(
    raop_client, raop_state, raop_usecase, require_auth
):
    raop_usecase.require_auth(require_auth)

    await raop_client.stream.stream_file(data_path("audio_10_frames.wav"))

    assert raop_state.auth_setup_performed == require_auth
    assert await audio_matches(raop_state.raw_audio, frames=10)


@pytest.mark.parametrize(
    "raop_properties,raop_server_password,raop_client_password",
    [
        ({"et": "0"}, "test", "test"),
        ({"et": "0"}, None, None),
        ({"et": "0"}, "test", None),
    ],
)
async def test_stream_with_password(
    raop_state,
    raop_usecase,
    raop_conf,
    raop_server_password,
    raop_client_password,
    event_loop,
):
    raop_usecase.password(raop_server_password)

    raop_service = raop_conf.get_service(Protocol.RAOP)
    if raop_service:
        raop_service.password = raop_client_password

    expect_error = raop_server_password != raop_client_password

    client = await connect(raop_conf, loop=event_loop)
    try:
        await client.stream.stream_file(data_path("audio_10_frames.wav"))
        assert not expect_error
    except AuthenticationError as e:
        assert expect_error
    except Exception as e:
        assert False
    finally:
        await asyncio.gather(*client.close())


@pytest.mark.parametrize(
    "raop_properties,drop_packets,enable_retransmission",
    [({"et": "0"}, 0, True), ({"et": "0"}, 2, False), ({"et": "0"}, 2, True)],
)
async def test_stream_retransmission(
    raop_client, raop_state, raop_usecase, drop_packets, enable_retransmission
):
    raop_usecase.retransmissions_enabled(enable_retransmission)
    raop_usecase.drop_n_packets(drop_packets)

    await raop_client.stream.stream_file(data_path("audio_3_packets.wav"))

    # For stability reasons: wait for all packets to be received as it might take a few
    # extra runs for the event loop to catch up
    packets_to_receive = 3 if enable_retransmission else 1
    await until(lambda: len(raop_state.audio_packets) >= packets_to_receive)

    # If retransmissions are enabled, then we should always receive all packets in
    # the end (within reasons). If retransmissions are not enabled, then we should
    # start comparing the received audio stream after the amount of audio packets
    # dropped.
    start_frame = 0 if enable_retransmission else drop_packets * FRAMES_PER_PACKET
    assert await audio_matches(
        raop_state.raw_audio,
        frames=3 * FRAMES_PER_PACKET - start_frame,  # Total expected frame
        skip_frames=start_frame,  # Skipping first amount of frames
    )


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_push_updates(raop_client, playing_listener):
    await raop_client.stream.stream_file(data_path("only_metadata.wav"))

    # Initial idle + audio playing + back to idle
    await until(lambda: len(playing_listener.updates) == 3)

    idle = playing_listener.updates[0]
    assert idle.device_state == DeviceState.Idle
    assert idle.media_type == MediaType.Unknown

    playing = playing_listener.updates[1]
    assert playing.device_state == DeviceState.Playing
    assert playing.media_type == MediaType.Music
    assert playing.artist == "postlund"
    assert playing.title == "pyatv"
    assert playing.album == "raop"

    idle = playing_listener.updates[2]
    assert idle.device_state == DeviceState.Idle
    assert idle.media_type == MediaType.Unknown


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_push_updates_progress(raop_client, playing_listener):
    assert_features_in_state(
        raop_client.features.all_features(),
        PROGRESS_FIELDS,
        FeatureState.Unavailable,
    )

    await raop_client.stream.stream_file(data_path("static_3sec.ogg"))

    # Initial idle + audio playing + back to idle
    await until(lambda: len(playing_listener.updates) == 3)

    playing = playing_listener.updates[1]
    assert playing.device_state == DeviceState.Playing
    assert playing.position == 0
    assert playing.total_time == 3

    assert_features_in_state(
        playing_listener.all_features,
        PROGRESS_FIELDS,
        FeatureState.Available,
    )


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_metadata_features(raop_client, playing_listener):
    # All features should be unavailable when nothing is playing
    assert_features_in_state(
        raop_client.features.all_features(),
        METADATA_FIELDS,
        FeatureState.Unavailable,
    )

    # StreamFile should be available for streaming
    assert (
        raop_client.features.get_feature(FeatureName.StreamFile).state
        == FeatureState.Available
    )
    await raop_client.stream.stream_file(data_path("only_metadata.wav"))

    # Use a listener to catch when something starts playing and save that as it's
    # too late to verify when stream_file returns (idle state will be reported).
    await until(lambda: playing_listener.all_features)

    # When playing, everything should be available
    assert_features_in_state(
        playing_listener.all_features,
        METADATA_FIELDS,
        FeatureState.Available,
    )


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_remote_control_features(raop_client, playing_listener):
    assert_features_in_state(
        raop_client.features.all_features(),
        REMOTE_CONTROL_FIELDS,
        FeatureState.Unavailable,
    )

    # Start playback in the background
    future = asyncio.ensure_future(
        raop_client.stream.stream_file(data_path("audio_3_packets.wav"))
    )

    # Wait for device to move to playing state and verify feature state
    await playing_listener.playing_event.wait()
    assert_features_in_state(
        raop_client.features.all_features(),
        REMOTE_CONTROL_FIELDS,
        FeatureState.Available,
    )

    await future

    # Playback finished so no controls should be available
    assert_features_in_state(
        raop_client.features.all_features(),
        REMOTE_CONTROL_FIELDS,
        FeatureState.Unavailable,
    )


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_sync_packets(raop_client, raop_state):
    await raop_client.stream.stream_file(data_path("only_metadata.wav"))

    # TODO: This test doesn't really test anything, just makes sure that sync packets
    # are received. Expand this test in the future.
    await until(lambda: raop_state.sync_packets_received > 5)


@pytest.mark.parametrize(
    "raop_properties,feedback_supported", [({"et": "0"}, True), ({"et": "0"}, False)]
)
async def test_send_feedback(raop_client, raop_usecase, raop_state, feedback_supported):
    raop_usecase.feedback_enabled(feedback_supported)

    await raop_client.stream.stream_file(data_path("audio_3_packets.wav"))

    # One request is sent to see if feedback is supported, then additional requests are
    # only sent if actually supported
    if feedback_supported:
        assert raop_state.feedback_packets_received > 1
    else:
        assert raop_state.feedback_packets_received == 1


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_set_volume_prior_to_streaming(raop_client, raop_state):
    # Initial client sound level
    assert math.isclose(raop_client.audio.volume, 33.0)

    await raop_client.audio.set_volume(60)
    assert math.isclose(raop_client.audio.volume, 60)

    await raop_client.stream.stream_file(data_path("only_metadata.wav"))
    assert math.isclose(raop_state.volume, -12.0)


@pytest.mark.parametrize(
    "raop_properties,initial_level_supported,sender_expected,receiver_expected",
    [
        # Device supports default level: use that
        ({"et": "0"}, True, 50.0, -15.0),
        # Device does NOT support default level: use pyatv default
        ({"et": "0"}, False, 33.0, -20.1),
    ],
)
async def test_use_default_volume_from_device(
    raop_client,
    raop_state,
    raop_usecase,
    initial_level_supported,
    sender_expected,
    receiver_expected,
):
    raop_usecase.initial_audio_level_supported(initial_level_supported)

    # Prior to streaming, we don't know the volume of the receiver so return default level
    assert math.isclose(raop_client.audio.volume, 33.0)

    # Default level on remote device
    assert math.isclose(raop_state.volume, -15.0)

    await raop_client.stream.stream_file(data_path("only_metadata.wav"))

    # Level on the client and receiver should match now
    assert math.isclose(raop_state.volume, receiver_expected)
    assert math.isclose(raop_client.audio.volume, sender_expected)


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_set_volume_during_playback(raop_client, raop_state, playing_listener):
    # Set maximum volume as initial volume
    await raop_client.audio.set_volume(100.0)

    # Start playback in the background
    future = asyncio.ensure_future(
        raop_client.stream.stream_file(data_path("audio_3_packets.wav"))
    )

    # Wait for device to move to playing state and verify volume
    await playing_listener.playing_event.wait()
    assert math.isclose(raop_state.volume, -0.0)

    # Change volume, which we now know will happen during playback
    await raop_client.audio.set_volume(50.0)
    assert math.isclose(raop_state.volume, -15.0)

    await future


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_volume_features(raop_client):
    assert_features_in_state(
        raop_client.features.all_features(), VOLUME_FIELDS, FeatureState.Available
    )


@pytest.mark.parametrize(
    "raop_properties,iface", [({"et": "0"}, "remote_control"), ({"et": "0"}, "audio")]
)
async def test_volume_up_volume_down(raop_client, iface):
    # Only test on the client as other tests should confirm that it is set correctly
    # on the receiver
    await raop_client.audio.set_volume(95.0)

    volume_interface = getattr(raop_client, iface)

    # Increase by 5% if volume_up is called
    await volume_interface.volume_up()
    assert math.isclose(raop_client.audio.volume, 100.0)

    # Stop at max level without any error
    await volume_interface.volume_up()
    assert math.isclose(raop_client.audio.volume, 100.0)

    await raop_client.audio.set_volume(5.0)

    # Decrease by 5% if volume_down is called
    await volume_interface.volume_down()
    assert math.isclose(raop_client.audio.volume, 0.0)

    # Stop at min level without any error
    await volume_interface.volume_down()
    assert math.isclose(raop_client.audio.volume, 0.0)


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_only_allow_one_stream_at_the_time(raop_client):
    # This is not pretty, but the idea is to start two concurrent streaming tasks, wait
    # for them to finish and verify that one of them raised an exception. This is to
    # avoid making any assumptions regarding in which order they are scheduled on the
    # event loop.
    result = await asyncio.gather(
        raop_client.stream.stream_file(data_path("audio_3_packets.wav")),
        raop_client.stream.stream_file(data_path("only_metadata.wav")),
        return_exceptions=True,
    )

    result.remove(None)  # Should be one None for success and one exception
    assert len(result) == 1
    assert isinstance(result[0], exceptions.InvalidStateError)


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_muted_volume_from_receiver(raop_client, raop_state, raop_usecase):
    raop_usecase.initial_audio_level_supported(True)
    raop_state.volume = -144.0

    await raop_client.stream.stream_file(data_path("only_metadata.wav"))

    assert math.isclose(raop_client.audio.volume, 0.0)


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_mute_volume_from_client(raop_client, raop_state):
    await raop_client.audio.set_volume(0.0)

    await raop_client.stream.stream_file(data_path("only_metadata.wav"))

    assert math.isclose(raop_state.volume, -144.0)


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_device_not_supporting_info_requests(raop_client, raop_usecase):
    raop_usecase.supports_info(False)

    # Should just not crash with an error if endpoint is not supported
    await raop_client.stream.stream_file(data_path("only_metadata.wav"))


@pytest.mark.parametrize("raop_properties", [({"et": "0"})])
async def test_teardown_called_after_playback(raop_client, raop_state):
    await raop_client.stream.stream_file(data_path("only_metadata.wav"))
    assert raop_state.teardown_called


@pytest.mark.parametrize("raop_properties", [({"et": "0", "md": "0"})])
async def test_custom_metadata(raop_client, raop_state):
    metadata = MediaMetadata(title="A", artist="B", album="C")

    await raop_client.stream.stream_file(
        data_path("only_metadata.wav"), metadata=metadata
    )

    # Note: duration cannot be changed here
    assert raop_state.metadata.title == "A"
    assert raop_state.metadata.artist == "B"
    assert raop_state.metadata.album == "C"


@pytest.mark.parametrize("raop_properties", [({"et": "0", "md": "0"})])
async def test_stream_from_buffer(raop_client, raop_state):
    with io.open(data_path("audio_1_packet_metadata.wav"), "rb") as source_file:
        await raop_client.stream.stream_file(source_file)

    assert raop_state.metadata.artist == "postlund"
    assert raop_state.metadata.album == "raop"
    assert raop_state.metadata.title == "pyatv"

    # TODO: stability problems here, need to look into it
    # assert await audio_matches(raop_state.raw_audio, frames=FRAMES_PER_PACKET)


@pytest.mark.parametrize(
    "raop_properties,button",
    [({"et": "0"}, "stop"), ({"et": "0"}, "pause")],  # We treat pause as stop for now
)
async def test_stop_playback(raop_client, raop_state, button):
    async def _fake_sleep(time: float = None, loop=None):
        async def dummy():
            pass

        await getattr(raop_client.remote_control, button)()
        await asyncio.ensure_future(dummy())

    # The idea here is to simulate calling "stop" after the first frame has been sent,
    # i.e. after the first "sleep" has been made. It's a bit tied to implementation
    # details but good enough.
    stub_sleep(fn=_fake_sleep)

    await raop_client.stream.stream_file(data_path("audio_3_packets.wav"))

    assert len(raop_state.raw_audio) >= ONE_FRAME_IN_BYTES


@pytest.mark.parametrize(
    "files, raop_properties", [(["only_metadata.wav"], {"et": "0", "md": "0"})]
)
async def test_stream_metadata_from_http(
    raop_client, raop_state, data_webserver, files
):
    file_url = data_webserver + files[0]
    await raop_client.stream.stream_file(file_url)

    assert raop_state.metadata.artist == "postlund"
    assert raop_state.metadata.album == "raop"
    assert raop_state.metadata.title == "pyatv"


@pytest.mark.parametrize("raop_properties", [({"et": "0", "md": "0"})])
async def test_stream_volume_set_after_stream_start(
    raop_client, raop_state, raop_usecase
):
    raop_usecase.delayed_set_volume(True)

    volume = 9

    await raop_client.audio.set_volume(volume)
    await raop_client.stream.stream_file(data_path("audio_1_packet_metadata.wav"))

    assert math.isclose(raop_client.audio.volume, volume)
    assert math.isclose(dbfs_to_pct(raop_state.volume), volume)
