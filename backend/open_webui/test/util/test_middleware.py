from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from open_webui.utils.middleware import (
    RESPONSES_STREAM_STATUS_ACTION,
    RESPONSES_STREAM_WAITING_DESCRIPTION,
    build_responses_stream_status_event,
    emit_stream_sideband_event,
    extract_stream_sideband_event,
    handle_responses_streaming_event,
    sanitize_direct_stream_status_event,
)


class TestExtractStreamSidebandEvent:
    def test_extracts_wrapped_open_webui_event_without_consuming_chunk(self):
        payload = {
            'event': {
                'type': 'status',
                'data': {'description': 'agent planning', 'done': False},
            }
        }

        event, consume_chunk = extract_stream_sideband_event(payload)

        assert event == payload['event']
        assert consume_chunk is False

    def test_extracts_direct_provider_status_event_and_consumes_chunk(self):
        payload = {
            'type': 'status',
            'data': {'description': 'coder sub-agent running', 'done': False},
        }

        event, consume_chunk = extract_stream_sideband_event(payload)

        assert event == payload
        assert consume_chunk is True

    def test_ignores_non_event_stream_chunks(self):
        payload = {
            'choices': [
                {
                    'delta': {'content': 'hello'},
                }
            ]
        }

        event, consume_chunk = extract_stream_sideband_event(payload)

        assert event is None
        assert consume_chunk is False


class TestEmitStreamSidebandEvent:
    @pytest.mark.asyncio
    async def test_emits_direct_provider_status_event(self):
        request = SimpleNamespace(state=SimpleNamespace())
        event_emitter = AsyncMock()
        payload = {
            'type': 'status',
            'data': {'description': 'planner started', 'done': False},
        }

        consumed = await emit_stream_sideband_event(request, event_emitter, payload)

        assert consumed is True
        event_emitter.assert_awaited_once_with(payload)

    @pytest.mark.asyncio
    async def test_does_not_consume_wrapped_events(self):
        request = SimpleNamespace(state=SimpleNamespace())
        event_emitter = AsyncMock()
        payload = {
            'event': {
                'type': 'status',
                'data': {'description': 'tool status', 'done': False},
            }
        }

        consumed = await emit_stream_sideband_event(request, event_emitter, payload)

        assert consumed is False
        event_emitter.assert_awaited_once_with(payload['event'])

    @pytest.mark.asyncio
    async def test_emits_sanitized_status_for_direct_connections(self):
        request = SimpleNamespace(state=SimpleNamespace(direct=True))
        event_emitter = AsyncMock()
        payload = {
            'type': 'status',
            'data': {
                'description': 'provider status',
                'done': False,
                'hidden': False,
                'urls': ['https://should-not-pass-through.example'],
            },
        }

        consumed = await emit_stream_sideband_event(request, event_emitter, payload)

        assert consumed is True
        event_emitter.assert_awaited_once_with(
            {
                'type': 'status',
                'data': {
                    'description': 'provider status',
                    'done': False,
                    'hidden': False,
                },
            }
        )

    @pytest.mark.asyncio
    async def test_skips_non_status_events_for_direct_connections(self):
        request = SimpleNamespace(state=SimpleNamespace(direct=True))
        event_emitter = AsyncMock()
        payload = {
            'event': {
                'type': 'chat:message',
                'data': {'content': 'should be ignored'},
            }
        }

        consumed = await emit_stream_sideband_event(request, event_emitter, payload)

        assert consumed is False
        event_emitter.assert_not_awaited()


class TestSanitizeDirectStreamStatusEvent:
    def test_returns_none_for_non_status_event(self):
        event = {'type': 'chat:message', 'data': {'content': 'hello'}}

        assert sanitize_direct_stream_status_event(event) is None

    def test_keeps_only_safe_status_fields(self):
        event = {
            'type': 'status',
            'data': {
                'description': 'sub-agent working',
                'done': False,
                'hidden': True,
                'action': 'custom_agent_progress',
                'query': 'sub-agent-1',
                'count': 2,
                'queries': ['coder', 'ops'],
                'urls': ['https://ignored.example'],
                'items': [{'label': 'ignored'}],
            },
        }

        assert sanitize_direct_stream_status_event(event) == {
            'type': 'status',
            'data': {
                'description': 'sub-agent working',
                'done': False,
                'hidden': True,
                'action': 'custom_agent_progress',
                'query': 'sub-agent-1',
                'count': 2,
                'queries': ['coder', 'ops'],
            },
        }


class TestBuildResponsesStreamStatusEvent:
    def test_builds_visible_waiting_status(self):
        assert build_responses_stream_status_event() == {
            'type': 'status',
            'data': {
                'action': RESPONSES_STREAM_STATUS_ACTION,
                'description': RESPONSES_STREAM_WAITING_DESCRIPTION,
                'done': False,
                'hidden': False,
            },
        }

    def test_builds_hidden_completion_status(self):
        assert build_responses_stream_status_event(hidden=True) == {
            'type': 'status',
            'data': {
                'action': RESPONSES_STREAM_STATUS_ACTION,
                'description': RESPONSES_STREAM_WAITING_DESCRIPTION,
                'done': True,
                'hidden': True,
            },
        }


class TestHandleResponsesStreamingEvent:
    def test_response_completed_with_failed_status_surfaces_error(self):
        output, metadata = handle_responses_streaming_event(
            {
                'type': 'response.completed',
                'response': {
                    'id': 'resp_1',
                    'status': 'failed',
                    'output': [
                        {
                            'type': 'message',
                            'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': 'partial commentary'}],
                            'phase': 'commentary',
                            'status': 'completed',
                        }
                    ],
                    'usage': {'total_tokens': 42},
                },
            },
            [],
        )

        assert output == [
            {
                'type': 'message',
                'role': 'assistant',
                'content': [{'type': 'output_text', 'text': 'partial commentary'}],
                'phase': 'commentary',
                'status': 'completed',
            }
        ]
        assert metadata == {
            'usage': {'total_tokens': 42},
            'done': True,
            'response_id': 'resp_1',
            'error': {
                'message': "Responses API stream ended with status 'failed'.",
                'type': 'responses_api_error',
                'status': 'failed',
            },
        }
