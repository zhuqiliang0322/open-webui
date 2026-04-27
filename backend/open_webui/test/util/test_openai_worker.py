import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_webui.routers import openai


def test_resolve_openclaw_worker_api_config_prefers_connection_values(monkeypatch):
    monkeypatch.setattr(openai, 'OPENCLAW_WORKER_API_BASE_URL', 'http://127.0.0.1:8090')
    monkeypatch.setattr(openai, 'OPENCLAW_WORKER_API_TOKEN', 'env-token')

    base_url, token = openai.resolve_openclaw_worker_api_config(
        {
            'worker_api_base_url': ' http://worker.internal:9000/ ',
            'worker_api_token': ' connection-token ',
        }
    )

    assert base_url == 'http://worker.internal:9000'
    assert token == 'connection-token'


def test_resolve_openclaw_worker_api_config_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(openai, 'OPENCLAW_WORKER_API_BASE_URL', 'http://127.0.0.1:8090')
    monkeypatch.setattr(openai, 'OPENCLAW_WORKER_API_TOKEN', 'env-token')

    base_url, token = openai.resolve_openclaw_worker_api_config({})

    assert base_url == 'http://127.0.0.1:8090'
    assert token == 'env-token'

def test_extract_openclaw_worker_prompt_prefers_user_message_text():
    prompt = openai.extract_openclaw_worker_prompt(
        {
            'input': [
                {
                    'type': 'message',
                    'role': 'user',
                    'content': [
                        {'type': 'input_text', 'text': 'simulate a task'},
                        {'type': 'input_text', 'text': 'that requires multiple agents'},
                    ],
                }
            ]
        }
    )

    assert prompt == 'simulate a task\nthat requires multiple agents'


def test_extract_openclaw_worker_prompt_reads_chat_completion_messages():
    prompt = openai.extract_openclaw_worker_prompt(
        {
            'messages': [
                {'role': 'system', 'content': 'ignored'},
                {'role': 'user', 'content': 'simulate a task'},
                {'role': 'user', 'content': [{'type': 'text', 'text': 'that requires multiple agents'}]},
            ]
        }
    )

    assert prompt == 'simulate a task\n\nthat requires multiple agents'


def test_should_dispatch_openclaw_worker_for_multi_agent_prompt_without_estimate():
    assert openai.should_dispatch_openclaw_worker(
        'simulate a task that requires multiple agents to collaborate',
        None,
    )


def test_should_dispatch_openclaw_worker_for_generic_poster_generation_prompt():
    assert openai.should_dispatch_openclaw_worker(
        '生成一张新年海报 2K分辨率 3d 效果，高级但中国风',
        {'recommendedJobType': 'visual_batch'},
    )


def test_should_not_treat_copywriting_request_as_visual_generation_candidate():
    assert not openai.looks_like_openclaw_visual_generation_candidate('写一段新年海报文案，要高级一点。')


def test_should_dispatch_openclaw_worker_for_visual_attachment_without_estimate():
    assert openai.should_dispatch_openclaw_worker('', None, has_visual_attachments=True)


@pytest.mark.asyncio
async def test_resolve_openclaw_worker_attachments_materializes_data_url(monkeypatch, tmp_path):
    monkeypatch.setattr(openai, 'CACHE_DIR', tmp_path)

    image_data_url = (
        'data:image/png;base64,'
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6N3ioAAAAASUVORK5CYII='
    )

    attachments = await openai.resolve_openclaw_worker_attachments(
        {
            'input': [
                {
                    'type': 'message',
                    'role': 'user',
                    'content': [
                        {'type': 'input_image', 'image_url': image_data_url},
                    ],
                }
            ]
        }
    )

    assert len(attachments) == 1
    attachment = attachments[0]

    assert attachment['type'] == 'image/png'
    assert attachment['url'] is None
    assert attachment['name'] == 'openclaw-worker-input.png'
    assert attachment['path']
    assert Path(attachment['path']).is_file()
    assert Path(attachment['path']).parent == tmp_path / 'openclaw' / 'worker_inputs'
    assert Path(attachment['path']).suffix == '.png'


def test_prune_openclaw_worker_input_cache_removes_expired_and_excess_files(tmp_path, monkeypatch):
    cache_root = tmp_path / 'worker_inputs'
    cache_root.mkdir()

    old_path = cache_root / 'old.png'
    keep_a = cache_root / 'keep-a.png'
    keep_b = cache_root / 'keep-b.png'
    trim_path = cache_root / 'trim.png'

    old_path.write_bytes(b'old')
    keep_a.write_bytes(b'a')
    keep_b.write_bytes(b'b')
    trim_path.write_bytes(b'trim')

    now = 1_777_000_000
    monkeypatch.setattr(openai.time, 'time', lambda: now)
    timestamps = {
        old_path: now - 100,
        keep_a: now - 10,
        keep_b: now - 5,
        trim_path: now - 1,
    }
    for path, mtime in timestamps.items():
        path.touch()
        os.utime(path, (mtime, mtime))

    openai.prune_openclaw_worker_input_cache(
        cache_root,
        max_files=2,
        max_age_seconds=30,
    )

    assert not old_path.exists()
    assert not keep_a.exists()
    assert keep_b.exists()
    assert trim_path.exists()


@pytest.mark.asyncio
async def test_resolve_openclaw_worker_attachments_skips_inaccessible_file_ids(monkeypatch):
    class DummyFile:
        user_id = 'owner-2'
        filename = 'avatar.png'
        path = '/Users/panda/open-webui/.data/uploads/test-avatar.png'
        meta = {'content_type': 'image/png'}

    class DummyUser:
        id = 'user-1'
        role = 'user'

    async def fake_get_file_by_id(file_id, db=None):
        assert file_id == '3a1c4e65-ad4e-4096-bb39-13fd52917578'
        return DummyFile()

    async def fake_has_access_to_file(file_id, access_type, user, db=None):
        assert file_id == '3a1c4e65-ad4e-4096-bb39-13fd52917578'
        assert access_type == 'read'
        assert user.id == 'user-1'
        return False

    monkeypatch.setattr(openai.Files, 'get_file_by_id', fake_get_file_by_id)
    monkeypatch.setattr(openai, 'has_access_to_file', fake_has_access_to_file)

    attachments = await openai.resolve_openclaw_worker_attachments(
        {
            'input': [
                {
                    'type': 'message',
                    'role': 'user',
                    'content': [
                        {
                            'type': 'input_image',
                            'image_url': '3a1c4e65-ad4e-4096-bb39-13fd52917578',
                        },
                    ],
                }
            ]
        },
        user=DummyUser(),
    )

    assert attachments == []


def test_should_not_dispatch_openclaw_worker_for_title_generation_prompt():
    prompt = """### Task:
Generate a concise, 3-5 word title with an emoji summarizing the chat history.
### Output:
JSON format: { "title": "your concise title here" }
### Chat History:
<chat_history>
USER: please use multiple agents to collaborate on this task
</chat_history>"""

    assert openai.is_openclaw_worker_internal_metadata_prompt(prompt)
    assert not openai.should_dispatch_openclaw_worker(prompt, {'isMultiAgent': True})


def test_should_not_dispatch_openclaw_worker_for_tags_generation_prompt():
    prompt = """### Task:
Generate 1-3 broad tags categorizing the main themes of the chat history, along with 1-3 more specific subtopic tags.
### Output:
JSON format: { "tags": ["tag1", "tag2", "tag3"] }
### Chat History:
<chat_history>
USER: all agents should collaborate on this
</chat_history>"""

    assert openai.is_openclaw_worker_internal_metadata_prompt(prompt)
    assert not openai.should_dispatch_openclaw_worker(prompt, None)


def test_render_openclaw_worker_ack_includes_job_id():
    ack = openai.render_openclaw_worker_ack(
        {
            'id': 'job-web-001',
            'agent_id': 'ops',
            'selected_model_public': 'qwen3.5-9b',
            'estimate': {
                'selectedAgent': 'ops',
                'routeReason': ['multi-agent coordination requested'],
                'preferredInitialBatch': ['ops'],
            },
        }
    )

    assert '<!-- OpenClaw Worker | job id: `job-web-001` -->' in ack
    assert '已接到你的请求，正在按协作方式处理。' in ack
    assert '我会先安排 ops 开始。' in ack
    assert '当前安排、子任务进度和最终结果会继续显示在这条消息里。' in ack


def test_build_openclaw_worker_subagent_progress_reads_transcript(tmp_path):
    root = tmp_path / 'OpenClaw'
    sessions_dir = root / 'config' / 'agents' / 'main' / 'sessions'
    transcript_path = sessions_dir / 'worker-session.jsonl'
    report_path = root / 'work' / 'reports' / 'jobs' / '2026-04-19' / 'job-web-001.json'

    sessions_dir.mkdir(parents=True)
    report_path.parent.mkdir(parents=True)
    (root / 'config' / 'openclaw.json').write_text('{}', encoding='utf-8')
    report_path.write_text('{}', encoding='utf-8')
    (sessions_dir / 'sessions.json').write_text(
        json.dumps(
            {
                'agent:main:worker:job-web-001': {
                    'sessionFile': str(transcript_path),
                }
            }
        ),
        encoding='utf-8',
    )
    transcript_path.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [
                                {
                                    'type': 'toolCall',
                                    'id': 'call-1',
                                    'name': 'sessions_spawn',
                                    'arguments': {
                                        'agentId': 'coder',
                                        'task': '给出 2 条本地 CLI 自检命令。',
                                    },
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'toolResult',
                            'toolName': 'sessions_spawn',
                            'toolCallId': 'call-1',
                            'details': {
                                'status': 'accepted',
                                'childSessionKey': 'agent:coder:subagent:child-1',
                            },
                            'content': [],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [
                                {
                                    'type': 'toolCall',
                                    'id': 'call-2',
                                    'name': 'sessions_spawn',
                                    'arguments': {
                                        'agentId': 'visual',
                                        'task': '给出 3 行显示结构。',
                                    },
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'toolResult',
                            'toolName': 'sessions_spawn',
                            'toolCallId': 'call-2',
                            'details': {
                                'status': 'accepted',
                                'childSessionKey': 'agent:visual:subagent:child-2',
                            },
                            'content': [],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'user',
                            'content': [
                                {
                                    'type': 'text',
                                    'text': '\n'.join(
                                        [
                                            '<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>',
                                            '[Internal task completion event]',
                                            'source: subagent',
                                            'session_key: agent:coder:subagent:child-1',
                                            'task: 给出 2 条本地 CLI 自检命令。',
                                            'status: completed successfully',
                                            '<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>',
                                            '命令 A',
                                            '<<<END_UNTRUSTED_CHILD_RESULT>>>',
                                            '<<<END_OPENCLAW_INTERNAL_CONTEXT>>>',
                                        ]
                                    ),
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    progress = openai.build_openclaw_worker_subagent_progress(
        {
            'agent_id': 'main',
            'worker_session_key': 'agent:main:worker:job-web-001',
            'report_json': str(report_path),
        }
    )

    assert progress == {
        'startedCount': 2,
        'completedCount': 1,
        'activeCount': 1,
        'items': [
            {
                'sessionKey': 'agent:coder:subagent:child-1',
                'agentId': 'coder',
                'task': '给出 2 条本地 CLI 自检命令。',
                'state': 'completed',
                'status': 'completed successfully',
                'resultPreview': '命令 A',
            },
            {
                'sessionKey': 'agent:visual:subagent:child-2',
                'agentId': 'visual',
                'task': '给出 3 行显示结构。',
                'state': 'running',
                'status': '',
                'resultPreview': '',
            },
        ],
    }


def test_build_openclaw_worker_subagent_progress_falls_back_to_openresponses_transcript(tmp_path):
    root = tmp_path / 'OpenClaw'
    sessions_dir = root / 'config' / 'agents' / 'main' / 'sessions'
    worker_transcript_path = sessions_dir / 'worker-session.jsonl'
    visible_transcript_path = sessions_dir / 'visible-session.jsonl'
    report_path = root / 'work' / 'reports' / 'jobs' / '2026-04-19' / 'job-web-002.json'

    sessions_dir.mkdir(parents=True)
    report_path.parent.mkdir(parents=True)
    (root / 'config' / 'openclaw.json').write_text('{}', encoding='utf-8')
    report_path.write_text('{}', encoding='utf-8')
    (sessions_dir / 'sessions.json').write_text(
        json.dumps(
            {
                'agent:main:worker:job-web-002': {
                    'sessionFile': str(worker_transcript_path),
                    'startedAt': 1_776_583_095_000,
                    'updatedAt': 1_776_583_175_000,
                },
                'agent:main:openresponses:webchat-1': {
                    'sessionFile': str(visible_transcript_path),
                    'startedAt': 1_776_583_100_000,
                    'updatedAt': 1_776_583_180_000,
                },
            }
        ),
        encoding='utf-8',
    )
    worker_transcript_path.write_text(
        json.dumps(
            {
                'type': 'message',
                'message': {
                    'role': 'assistant',
                    'content': [{'type': 'text', 'text': 'worker wrapper only'}],
                },
            }
        )
        + '\n',
        encoding='utf-8',
    )
    visible_transcript_path.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [
                                {
                                    'type': 'text',
                                    'text': '<!-- OpenClaw Worker | job id: `job-web-002` -->',
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [
                                {
                                    'type': 'toolCall',
                                    'id': 'call-1',
                                    'name': 'sessions_spawn',
                                    'arguments': {
                                        'agentId': 'heavy',
                                        'task': '给 1 条中文风险提示。',
                                    },
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'toolResult',
                            'toolName': 'sessions_spawn',
                            'toolCallId': 'call-1',
                            'details': {
                                'status': 'accepted',
                                'childSessionKey': 'agent:heavy:subagent:child-1',
                            },
                            'content': [],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'user',
                            'content': [
                                {
                                    'type': 'text',
                                    'text': '\n'.join(
                                        [
                                            '<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>',
                                            '[Internal task completion event]',
                                            'source: subagent',
                                            'session_key: agent:heavy:subagent:child-1',
                                            'task: 给 1 条中文风险提示。',
                                            'status: completed successfully',
                                            '<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>',
                                            '风险提示',
                                            '<<<END_UNTRUSTED_CHILD_RESULT>>>',
                                            '<<<END_OPENCLAW_INTERNAL_CONTEXT>>>',
                                        ]
                                    ),
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    progress = openai.build_openclaw_worker_subagent_progress(
        {
            'id': 'job-web-002',
            'agent_id': 'main',
            'worker_session_key': 'agent:main:worker:job-web-002',
            'report_json': str(report_path),
            'created_at': '2026-04-19T07:18:15+00:00',
        }
    )

    assert progress == {
        'startedCount': 1,
        'completedCount': 1,
        'activeCount': 0,
        'items': [
            {
                'sessionKey': 'agent:heavy:subagent:child-1',
                'agentId': 'heavy',
                'task': '给 1 条中文风险提示。',
                'state': 'completed',
                'status': 'completed successfully',
                'resultPreview': '风险提示',
            }
        ],
    }


def test_build_openclaw_worker_subagent_progress_ignores_auxiliary_openresponses_transcript(tmp_path):
    root = tmp_path / 'OpenClaw'
    sessions_dir = root / 'config' / 'agents' / 'main' / 'sessions'
    worker_transcript_path = sessions_dir / 'worker-session.jsonl'
    auxiliary_transcript_path = sessions_dir / 'title-session.jsonl'
    report_path = root / 'work' / 'reports' / 'jobs' / '2026-04-22' / 'job-web-003.json'

    sessions_dir.mkdir(parents=True)
    report_path.parent.mkdir(parents=True)
    (root / 'config' / 'openclaw.json').write_text('{}', encoding='utf-8')
    report_path.write_text('{}', encoding='utf-8')
    (sessions_dir / 'sessions.json').write_text(
        json.dumps(
            {
                'agent:main:worker:job-web-003': {
                    'sessionFile': str(worker_transcript_path),
                    'startedAt': 1_776_825_000_000,
                    'updatedAt': 1_776_825_005_000,
                },
                'agent:main:openresponses:webchat-aux': {
                    'sessionFile': str(auxiliary_transcript_path),
                    'startedAt': 1_776_825_001_000,
                    'updatedAt': 1_776_825_006_000,
                },
            }
        ),
        encoding='utf-8',
    )
    worker_transcript_path.write_text(
        json.dumps(
            {
                'type': 'message',
                'message': {
                    'role': 'assistant',
                    'content': [{'type': 'text', 'text': 'worker wrapper only'}],
                },
            }
        )
        + '\n',
        encoding='utf-8',
    )
    auxiliary_transcript_path.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'user',
                            'content': [
                                {
                                    'type': 'text',
                                    'text': '\n'.join(
                                        [
                                            '### Task:',
                                            'Generate a concise, 3-5 word title with an emoji summarizing the chat history.',
                                            '### Chat History:',
                                            '<chat_history>',
                                            'ASSISTANT: <!-- OpenClaw Worker | job id: `job-web-003` -->',
                                            '</chat_history>',
                                        ]
                                    ),
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [
                                {
                                    'type': 'toolCall',
                                    'id': 'call-1',
                                    'name': 'sessions_spawn',
                                    'arguments': {
                                        'agentId': 'heavy',
                                        'task': '给 1 条中文风险提示。',
                                    },
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'toolResult',
                            'toolName': 'sessions_spawn',
                            'toolCallId': 'call-1',
                            'details': {
                                'status': 'accepted',
                                'childSessionKey': 'agent:heavy:subagent:child-1',
                            },
                            'content': [],
                        },
                    }
                ),
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    progress = openai.build_openclaw_worker_subagent_progress(
        {
            'id': 'job-web-003',
            'agent_id': 'main',
            'worker_session_key': 'agent:main:worker:job-web-003',
            'report_json': str(report_path),
            'created_at': '2026-04-22T02:45:00+00:00',
        }
    )

    assert progress is None


def test_merge_openclaw_worker_subagent_progress_prefers_richer_transcript_snapshot():
    merged = openai.merge_openclaw_worker_subagent_progress(
        {
            'startedCount': 2,
            'completedCount': 0,
            'activeCount': 2,
            'items': [
                {
                    'sessionKey': 'agent:coder:subagent:child-1',
                    'agentId': 'coder',
                    'task': '旧快照任务',
                    'state': 'running',
                    'status': '',
                    'resultPreview': '',
                },
                {
                    'sessionKey': 'agent:heavy:subagent:child-2',
                    'agentId': 'heavy',
                    'task': '旧快照任务',
                    'state': 'running',
                    'status': '',
                    'resultPreview': '',
                },
            ],
        },
        {
            'startedCount': 2,
            'completedCount': 2,
            'activeCount': 0,
            'items': [
                {
                    'sessionKey': 'agent:coder:subagent:child-1',
                    'agentId': 'coder',
                    'task': '给出 2 条本地 CLI 自检命令。',
                    'state': 'completed',
                    'status': 'completed successfully',
                    'resultPreview': '命令 A',
                },
                {
                    'sessionKey': 'agent:heavy:subagent:child-2',
                    'agentId': 'heavy',
                    'task': '给 1 条中文风险提示。',
                    'state': 'completed',
                    'status': 'completed successfully',
                    'resultPreview': '风险提示',
                },
            ],
        },
    )

    assert merged == {
        'startedCount': 2,
        'completedCount': 2,
        'activeCount': 0,
        'items': [
            {
                'sessionKey': 'agent:coder:subagent:child-1',
                'agentId': 'coder',
                'task': '给出 2 条本地 CLI 自检命令。',
                'state': 'completed',
                'status': 'completed successfully',
                'resultPreview': '命令 A',
            },
            {
                'sessionKey': 'agent:heavy:subagent:child-2',
                'agentId': 'heavy',
                'task': '给 1 条中文风险提示。',
                'state': 'completed',
                'status': 'completed successfully',
                'resultPreview': '风险提示',
            },
        ],
    }


def test_normalize_openclaw_worker_job_payload_synthesizes_final_text_from_completed_subagents(tmp_path):
    root = tmp_path / 'OpenClaw'
    sessions_dir = root / 'config' / 'agents' / 'main' / 'sessions'
    transcript_path = sessions_dir / 'worker-session.jsonl'
    report_path = root / 'work' / 'reports' / 'jobs' / '2026-04-20' / 'job-web-003.json'

    sessions_dir.mkdir(parents=True)
    report_path.parent.mkdir(parents=True)
    (root / 'config' / 'openclaw.json').write_text('{}', encoding='utf-8')
    report_path.write_text('{}', encoding='utf-8')
    (sessions_dir / 'sessions.json').write_text(
        json.dumps(
            {
                'agent:main:worker:job-web-003': {
                    'sessionFile': str(transcript_path),
                }
            }
        ),
        encoding='utf-8',
    )
    transcript_path.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [
                                {
                                    'type': 'toolCall',
                                    'id': 'call-1',
                                    'name': 'sessions_spawn',
                                    'arguments': {
                                        'agentId': 'heavy',
                                        'task': '给 1 条中文风险提示。',
                                    },
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'toolResult',
                            'toolName': 'sessions_spawn',
                            'toolCallId': 'call-1',
                            'details': {
                                'status': 'accepted',
                                'childSessionKey': 'agent:heavy:subagent:child-1',
                            },
                            'content': [],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'user',
                            'content': [
                                {
                                    'type': 'text',
                                    'text': '\n'.join(
                                        [
                                            '<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>',
                                            '[Internal task completion event]',
                                            'source: subagent',
                                            'session_key: agent:heavy:subagent:child-1',
                                            'task: 给 1 条中文风险提示。',
                                            'status: completed successfully',
                                            '<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>',
                                            '风险提示',
                                            '<<<END_UNTRUSTED_CHILD_RESULT>>>',
                                            '<<<END_OPENCLAW_INTERNAL_CONTEXT>>>',
                                        ]
                                    ),
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    payload = openai.normalize_openclaw_worker_job_payload(
        {
            'agent_id': 'main',
            'worker_session_key': 'agent:main:worker:job-web-003',
            'report_json': str(report_path),
            'phase': 'completed',
            'status': 'succeeded',
            'final_visible_text': '多角色协调进行中。',
            'subagent_progress': {
                'startedCount': 1,
                'completedCount': 0,
                'activeCount': 1,
                'items': [
                    {
                        'sessionKey': 'agent:heavy:subagent:child-1',
                        'agentId': 'heavy',
                        'task': '旧快照任务',
                        'state': 'running',
                        'status': '',
                        'resultPreview': '',
                    }
                ],
            },
        }
    )

    assert payload['phase'] == 'completed'
    assert payload['status'] == 'succeeded'
    assert payload['subagent_progress']['completedCount'] == 1
    assert payload['subagent_progress']['activeCount'] == 0
    assert '多角色协作已收口。' in payload['final_visible_text']
    assert 'heavy：风险提示' in payload['final_visible_text']


def test_normalize_openclaw_worker_job_payload_keeps_waiting_job_running_when_active_subagents_remain():
    payload = openai.normalize_openclaw_worker_job_payload(
        {
            'phase': 'completed',
            'status': 'succeeded',
            'final_visible_text': '多角色协调进行中。',
            'subagent_progress': {
                'startedCount': 1,
                'completedCount': 0,
                'activeCount': 1,
                'items': [
                    {
                        'sessionKey': 'agent:coder:subagent:child-1',
                        'agentId': 'coder',
                        'task': '给出 2 条本地 CLI 自检命令。',
                        'state': 'running',
                        'status': '',
                        'resultPreview': '',
                    }
                ],
            },
        }
    )

    assert payload['phase'] == 'running'
    assert payload['status'] == 'running'
    assert payload['final_visible_text'] == ''


def test_normalize_openclaw_worker_job_payload_rewrites_thin_final_text():
    payload = openai.normalize_openclaw_worker_job_payload(
        {
            'phase': 'completed',
            'status': 'succeeded',
            'final_visible_text': '信息不足。',
            'subagent_progress': {
                'startedCount': 3,
                'completedCount': 2,
                'activeCount': 1,
                'items': [
                    {
                        'sessionKey': 'agent:heavy:subagent:child-1',
                        'agentId': 'heavy',
                        'task': '给 1 条中文风险提示。',
                        'state': 'completed',
                        'status': 'completed successfully',
                        'resultPreview': 'AI 输出需要人工复核。',
                    },
                    {
                        'sessionKey': 'agent:visual:subagent:child-2',
                        'agentId': 'visual',
                        'task': '给 3 行展示结构。',
                        'state': 'completed',
                        'status': 'completed successfully',
                        'resultPreview': '标题\n摘要\n详情',
                    },
                    {
                        'sessionKey': 'agent:coder:subagent:child-3',
                        'agentId': 'coder',
                        'task': '给 2 条本地检查命令。',
                        'state': 'failed',
                        'status': 'failed: gateway closed',
                        'resultPreview': '(no output)',
                    },
                ],
            },
        }
    )

    assert '主会话给出的最终结果过于简略' in payload['final_visible_text']
    assert 'heavy：AI 输出需要人工复核。' in payload['final_visible_text']
    assert 'visual：标题 摘要 详情' in payload['final_visible_text']
    assert 'coder：未成功返回可展示内容。状态：failed: gateway closed' in payload['final_visible_text']


def test_normalize_openclaw_worker_job_payload_resolves_artifact_files(tmp_path):
    root = tmp_path / 'OpenClaw'
    report_path = root / 'work' / 'reports' / 'jobs' / '2026-04-21' / 'job-web-artifacts.json'
    market_path = root / 'work' / 'reports' / 'ecom-demo' / 'market_conclusion.md'
    release_notes_path = root / 'work' / 'agent-workspaces' / 'release' / 'release_notes.md'

    report_path.parent.mkdir(parents=True)
    market_path.parent.mkdir(parents=True)
    release_notes_path.parent.mkdir(parents=True)
    (root / 'config').mkdir(parents=True)
    (root / 'config' / 'openclaw.json').write_text('{}', encoding='utf-8')
    report_path.write_text('{}', encoding='utf-8')
    market_path.write_text('# market', encoding='utf-8')
    release_notes_path.write_text('# release', encoding='utf-8')

    payload = openai.normalize_openclaw_worker_job_payload(
        {
            'report_json': str(report_path),
            'phase': 'completed',
            'status': 'succeeded',
            'final_visible_text': '\n'.join(
                [
                    '| **heavy** | `market_conclusion.md` |',
                    '| **release** | `release_notes.md` |',
                    '- `openclaw gateway status`',
                ]
            ),
        }
    )

    assert payload['resolved_artifacts'] == [
        {'label': 'market_conclusion.md', 'path': str(market_path.resolve())},
        {'label': 'release_notes.md', 'path': str(release_notes_path.resolve())},
    ]


def test_normalize_openclaw_worker_job_payload_prefers_contextual_artifact_path(tmp_path):
    root = tmp_path / 'OpenClaw'
    report_path = root / 'work' / 'reports' / 'jobs' / '2026-04-21' / 'job-web-release.json'
    release_validation_summary = (
        root / 'work' / 'agent-workspaces' / 'release' / 'reports' / 'validation-summary.md'
    )
    generic_validation_summary = root / 'work' / 'reports' / 'validation-summary.md'

    report_path.parent.mkdir(parents=True)
    release_validation_summary.parent.mkdir(parents=True)
    generic_validation_summary.parent.mkdir(parents=True, exist_ok=True)
    (root / 'config').mkdir(parents=True)
    (root / 'config' / 'openclaw.json').write_text('{}', encoding='utf-8')
    report_path.write_text('{}', encoding='utf-8')
    release_validation_summary.write_text('# release summary', encoding='utf-8')
    generic_validation_summary.write_text('# generic summary', encoding='utf-8')

    payload = openai.normalize_openclaw_worker_job_payload(
        {
            'report_json': str(report_path),
            'phase': 'completed',
            'status': 'succeeded',
            'final_visible_text': '| **release** | `reports/validation-summary.md` | ✅ |',
        }
    )

    assert payload['resolved_artifacts'] == [
        {
            'label': 'reports/validation-summary.md',
            'path': str(release_validation_summary.resolve()),
        }
    ]


def test_normalize_openclaw_worker_job_payload_includes_media_urls_as_artifacts(tmp_path):
    root = tmp_path / 'OpenClaw'
    report_path = root / 'work' / 'reports' / 'jobs' / '2026-04-23' / 'job-web-image.json'
    image_path = root / 'work' / 'tmp' / 'dreamina' / 'job-123' / 'poster.png'

    report_path.parent.mkdir(parents=True)
    image_path.parent.mkdir(parents=True)
    (root / 'config').mkdir(parents=True)
    (root / 'config' / 'openclaw.json').write_text('{}', encoding='utf-8')
    report_path.write_text('{}', encoding='utf-8')
    image_path.write_bytes(b'png')

    payload = openai.normalize_openclaw_worker_job_payload(
        {
            'report_json': str(report_path),
            'phase': 'completed',
            'status': 'succeeded',
            'final_visible_text': '新年海报已生成并下载完成。',
            'media_urls': [str(image_path.resolve())],
        }
    )

    assert payload['resolved_artifacts'] == [
        {'label': 'poster.png', 'path': str(image_path.resolve())},
    ]


def test_resolve_openclaw_worker_artifact_file_path_rejects_files_outside_worktree(tmp_path):
    root = tmp_path / 'OpenClaw'
    allowed_path = root / 'work' / 'reports' / 'demo' / 'market_conclusion.md'
    blocked_path = root / 'config' / 'openclaw.json'

    allowed_path.parent.mkdir(parents=True)
    blocked_path.parent.mkdir(parents=True)
    blocked_path.write_text('{}', encoding='utf-8')
    allowed_path.write_text('# market', encoding='utf-8')

    assert openai.resolve_openclaw_worker_artifact_file_path(str(allowed_path.resolve())) == allowed_path.resolve()
    assert openai.resolve_openclaw_worker_artifact_file_path(str(blocked_path.resolve())) is None


def test_build_openclaw_worker_artifact_response_headers_prefers_inline_markdown():
    media_type, headers = openai.build_openclaw_worker_artifact_response_headers(
        openai.Path('/tmp/market_conclusion.md')
    )

    assert media_type == 'text/markdown'
    assert headers['Content-Disposition'].startswith('inline;')


def test_build_openclaw_worker_artifact_response_headers_forces_download_for_binary():
    media_type, headers = openai.build_openclaw_worker_artifact_response_headers(
        openai.Path('/tmp/archive.zip')
    )

    assert media_type == 'application/zip'
    assert headers['Content-Disposition'].startswith('attachment;')


def test_normalize_openclaw_worker_job_payload_reconciles_failed_child_sessions(tmp_path):
    root = tmp_path / 'OpenClaw'
    main_sessions_dir = root / 'config' / 'agents' / 'main' / 'sessions'
    visual_sessions_dir = root / 'config' / 'agents' / 'visual' / 'sessions'
    ops_sessions_dir = root / 'config' / 'agents' / 'ops' / 'sessions'
    transcript_path = main_sessions_dir / 'worker-session.jsonl'
    visual_transcript_path = visual_sessions_dir / 'visual-child.jsonl'
    ops_transcript_path = ops_sessions_dir / 'ops-child.jsonl'
    report_path = root / 'work' / 'reports' / 'jobs' / '2026-04-20' / 'job-web-004.json'

    main_sessions_dir.mkdir(parents=True)
    visual_sessions_dir.mkdir(parents=True)
    ops_sessions_dir.mkdir(parents=True)
    report_path.parent.mkdir(parents=True)
    (root / 'config' / 'openclaw.json').write_text('{}', encoding='utf-8')
    report_path.write_text('{}', encoding='utf-8')
    (main_sessions_dir / 'sessions.json').write_text(
        json.dumps(
            {
                'agent:main:worker:job-web-004': {
                    'sessionFile': str(transcript_path),
                }
            }
        ),
        encoding='utf-8',
    )
    (visual_sessions_dir / 'sessions.json').write_text(
        json.dumps(
            {
                'agent:visual:subagent:child-1': {
                    'sessionFile': str(visual_transcript_path),
                    'status': 'failed',
                }
            }
        ),
        encoding='utf-8',
    )
    (ops_sessions_dir / 'sessions.json').write_text(
        json.dumps(
            {
                'agent:ops:subagent:child-2': {
                    'sessionFile': str(ops_transcript_path),
                    'status': 'failed',
                }
            }
        ),
        encoding='utf-8',
    )
    transcript_path.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [
                                {
                                    'type': 'toolCall',
                                    'id': 'call-1',
                                    'name': 'sessions_spawn',
                                    'arguments': {
                                        'agentId': 'visual',
                                        'task': '整理视觉规范。',
                                    },
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'toolResult',
                            'toolName': 'sessions_spawn',
                            'toolCallId': 'call-1',
                            'details': {
                                'status': 'accepted',
                                'childSessionKey': 'agent:visual:subagent:child-1',
                            },
                            'content': [],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'user',
                            'content': [
                                {
                                    'type': 'text',
                                    'text': '\n'.join(
                                        [
                                            '<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>',
                                            '[Internal task completion event]',
                                            'source: subagent',
                                            'session_key: agent:visual:subagent:child-1',
                                            'task: 整理视觉规范。',
                                            'status: completed successfully',
                                            '<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>',
                                            '<|channel>',
                                            '<<<END_UNTRUSTED_CHILD_RESULT>>>',
                                            '<<<END_OPENCLAW_INTERNAL_CONTEXT>>>',
                                        ]
                                    ),
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [
                                {
                                    'type': 'toolCall',
                                    'id': 'call-2',
                                    'name': 'sessions_spawn',
                                    'arguments': {
                                        'agentId': 'ops',
                                        'task': '跟踪排期。',
                                    },
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'toolResult',
                            'toolName': 'sessions_spawn',
                            'toolCallId': 'call-2',
                            'details': {
                                'status': 'accepted',
                                'childSessionKey': 'agent:ops:subagent:child-2',
                            },
                            'content': [],
                        },
                    }
                ),
            ]
        )
        + '\n',
        encoding='utf-8',
    )
    visual_transcript_path.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [{'type': 'text', 'text': '<|channel>'}],
                            'errorMessage': 'Model unloaded.',
                        },
                    }
                )
            ]
        )
        + '\n',
        encoding='utf-8',
    )
    ops_transcript_path.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [{'type': 'text', 'text': '我将先梳理现有流程，然后继续推进。'}],
                            'errorMessage': 'Context size has been exceeded.',
                        },
                    }
                )
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    payload = openai.normalize_openclaw_worker_job_payload(
        {
            'agent_id': 'main',
            'worker_session_key': 'agent:main:worker:job-web-004',
            'report_json': str(report_path),
            'phase': 'running',
            'status': 'running',
        }
    )

    assert payload['subagent_progress']['completedCount'] == 2
    assert payload['subagent_progress']['activeCount'] == 0
    assert payload['subagent_progress']['items'] == [
        {
            'sessionKey': 'agent:ops:subagent:child-2',
            'agentId': 'ops',
            'task': '跟踪排期。',
            'state': 'completed',
            'status': 'failed: Context size has been exceeded.',
            'resultPreview': '',
        },
        {
            'sessionKey': 'agent:visual:subagent:child-1',
            'agentId': 'visual',
            'task': '整理视觉规范。',
            'state': 'completed',
            'status': 'failed: Model unloaded.',
            'resultPreview': '',
        },
    ]


def test_normalize_openclaw_worker_job_payload_fallback_result_reflects_child_failures(tmp_path):
    root = tmp_path / 'OpenClaw'
    main_sessions_dir = root / 'config' / 'agents' / 'main' / 'sessions'
    visual_sessions_dir = root / 'config' / 'agents' / 'visual' / 'sessions'
    transcript_path = main_sessions_dir / 'worker-session.jsonl'
    visual_transcript_path = visual_sessions_dir / 'visual-child.jsonl'
    report_path = root / 'work' / 'reports' / 'jobs' / '2026-04-20' / 'job-web-005.json'

    main_sessions_dir.mkdir(parents=True)
    visual_sessions_dir.mkdir(parents=True)
    report_path.parent.mkdir(parents=True)
    (root / 'config' / 'openclaw.json').write_text('{}', encoding='utf-8')
    report_path.write_text('{}', encoding='utf-8')
    (main_sessions_dir / 'sessions.json').write_text(
        json.dumps(
            {
                'agent:main:worker:job-web-005': {
                    'sessionFile': str(transcript_path),
                }
            }
        ),
        encoding='utf-8',
    )
    (visual_sessions_dir / 'sessions.json').write_text(
        json.dumps(
            {
                'agent:visual:subagent:child-1': {
                    'sessionFile': str(visual_transcript_path),
                    'status': 'failed',
                }
            }
        ),
        encoding='utf-8',
    )
    transcript_path.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [
                                {
                                    'type': 'toolCall',
                                    'id': 'call-1',
                                    'name': 'sessions_spawn',
                                    'arguments': {
                                        'agentId': 'visual',
                                        'task': '整理视觉规范。',
                                    },
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'toolResult',
                            'toolName': 'sessions_spawn',
                            'toolCallId': 'call-1',
                            'details': {
                                'status': 'accepted',
                                'childSessionKey': 'agent:visual:subagent:child-1',
                            },
                            'content': [],
                        },
                    }
                ),
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'user',
                            'content': [
                                {
                                    'type': 'text',
                                    'text': '\n'.join(
                                        [
                                            '<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>',
                                            '[Internal task completion event]',
                                            'source: subagent',
                                            'session_key: agent:visual:subagent:child-1',
                                            'task: 整理视觉规范。',
                                            'status: completed successfully',
                                            '<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>',
                                            '<|channel>',
                                            '<<<END_UNTRUSTED_CHILD_RESULT>>>',
                                            '<<<END_OPENCLAW_INTERNAL_CONTEXT>>>',
                                        ]
                                    ),
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + '\n',
        encoding='utf-8',
    )
    visual_transcript_path.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'type': 'message',
                        'message': {
                            'role': 'assistant',
                            'content': [{'type': 'text', 'text': '<|channel>'}],
                            'errorMessage': 'Model unloaded.',
                        },
                    }
                )
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    payload = openai.normalize_openclaw_worker_job_payload(
        {
            'agent_id': 'main',
            'worker_session_key': 'agent:main:worker:job-web-005',
            'report_json': str(report_path),
            'phase': 'completed',
            'status': 'succeeded',
            'final_visible_text': '信息不足。',
        }
    )

    assert '<|channel>' not in payload['final_visible_text']
    assert 'visual：未成功返回可展示内容。状态：failed: Model unloaded.' in payload['final_visible_text']


@pytest.mark.asyncio
async def test_maybe_dispatch_openclaw_worker_returns_ack_for_chat_payload(monkeypatch):
    async def fake_fetch_openclaw_worker_json(worker_api_base_url, worker_api_token, method, path, payload=None):
        if path == '/estimate':
            return {
                'selectedAgent': 'ops',
                'recommendedJobType': 'agent_task',
                'routeReason': ['multi-agent coordination requested'],
                'preferredInitialBatch': ['ops'],
            }

        if path == '/jobs':
            return {
                'id': 'job-web-123',
                'agent_id': 'ops',
                'selected_model_public': 'qwen3.5-9b',
                'estimate': {
                    'selectedAgent': 'ops',
                    'routeReason': ['multi-agent coordination requested'],
                    'preferredInitialBatch': ['ops'],
                },
            }

        raise AssertionError(f'unexpected worker path: {path}')

    monkeypatch.setattr(openai, 'fetch_openclaw_worker_json', fake_fetch_openclaw_worker_json)

    result = await openai.maybe_dispatch_openclaw_worker(
        model='openclaw/main',
        payload={
            'messages': [
                {
                    'role': 'user',
                    'content': 'simulate a task that requires multiple agents to collaborate',
                }
            ]
        },
        url='http://127.0.0.1:18789/v1',
        api_config={
            'api_type': 'responses',
            'worker_api_base_url': 'http://127.0.0.1:8090',
        },
        source_channel='webchat',
    )

    assert result is not None
    assert result['handled'] is True
    assert result['job']['id'] == 'job-web-123'
    assert '<!-- OpenClaw Worker | job id: `job-web-123` -->' in result['ack']
    assert '已接到你的请求，正在按协作方式处理。' in result['ack']


@pytest.mark.asyncio
async def test_maybe_dispatch_openclaw_worker_accepts_non_responses_connection(monkeypatch):
    async def fake_fetch_openclaw_worker_json(worker_api_base_url, worker_api_token, method, path, payload=None):
        if path == '/estimate':
            return {
                'selectedAgent': 'visual',
                'recommendedJobType': 'visual_batch',
                'routeReason': ['visual-classified request'],
            }

        if path == '/jobs':
            return {
                'id': 'job-web-chat-123',
                'agent_id': payload['agent_id'],
                'selected_model_public': 'gemma-4-26b-a4b-it',
                'estimate': {
                    'selectedAgent': payload['agent_id'],
                    'routeReason': ['visual-classified request'],
                    'preferredInitialBatch': [payload['agent_id']],
                },
            }

        raise AssertionError(f'unexpected worker path: {path}')

    monkeypatch.setattr(openai, 'fetch_openclaw_worker_json', fake_fetch_openclaw_worker_json)

    result = await openai.maybe_dispatch_openclaw_worker(
        model='openclaw/main',
        payload={
            'messages': [
                {
                    'role': 'user',
                    'content': 'Generate one real Dreamina text-to-image now and show the image in this chat with the submit_id.',
                }
            ]
        },
        url='http://127.0.0.1:18789/v1',
        api_config={
            'worker_api_base_url': 'http://127.0.0.1:8090',
        },
        source_channel='openresponses',
    )

    assert result is not None
    assert result['handled'] is True
    assert result['job']['id'] == 'job-web-chat-123'
    assert '<!-- OpenClaw Worker | job id: `job-web-chat-123` -->' in result['ack']


@pytest.mark.asyncio
async def test_generate_chat_completion_dispatches_worker_for_chat_completion_connection(monkeypatch):
    captured = {}

    async def fake_get_model_by_id(model_id):
        return None

    async def fake_check_model_access(user, model_info, bypass_filter):
        return None

    async def fake_get_headers_and_cookies(request, url, key, api_config, metadata, user=None):
        return {}, {}

    async def fake_maybe_dispatch_openclaw_worker(**kwargs):
        captured.update(kwargs)
        return {
            'response': openai.build_openclaw_worker_response(
                kwargs['model'],
                '<!-- OpenClaw Worker | job id: `job-web-chat-456` -->\n\n已接到你的请求，正在按协作方式处理。',
            )
        }

    monkeypatch.setattr(openai.Models, 'get_model_by_id', fake_get_model_by_id)
    monkeypatch.setattr(openai, 'check_model_access', fake_check_model_access)
    monkeypatch.setattr(openai, 'get_headers_and_cookies', fake_get_headers_and_cookies)
    monkeypatch.setattr(openai, 'maybe_dispatch_openclaw_worker', fake_maybe_dispatch_openclaw_worker)

    request = SimpleNamespace(
        state=SimpleNamespace(),
        app=SimpleNamespace(
            state=SimpleNamespace(
                OPENAI_MODELS={'openclaw/main': {'urlIdx': 0}},
                config=SimpleNamespace(
                    OPENAI_API_CONFIGS={'0': {'worker_api_base_url': 'http://127.0.0.1:8090'}},
                    OPENAI_API_BASE_URLS=['http://127.0.0.1:18789/v1'],
                    OPENAI_API_KEYS=['test-key'],
                ),
            )
        ),
    )

    result = await openai.generate_chat_completion(
        request=request,
        form_data={
            'model': 'openclaw/main',
            'messages': [
                {
                    'role': 'user',
                    'content': '生成一张新年海报 2K分辨率 3d 效果，高级但中国风',
                }
            ],
        },
        user=SimpleNamespace(id='user-1', name='panda', email='panda@example.local', role='user'),
    )

    assert result['object'] == 'chat.completion'
    assert 'job-web-chat-456' in result['choices'][0]['message']['content']
    assert captured['model'] == 'openclaw/main'
    assert captured['payload']['messages'][0]['content'] == '生成一张新年海报 2K分辨率 3d 效果，高级但中国风'
    assert captured['api_config']['worker_api_base_url'] == 'http://127.0.0.1:8090'


@pytest.mark.asyncio
async def test_get_openclaw_worker_job_accepts_non_responses_connection(monkeypatch):
    async def fake_resolve_openai_model_connection(request, user, model):
        return None, None, None, None, {'worker_api_base_url': 'http://127.0.0.1:8090'}

    class FakeResponse:
        ok = True
        status = 200

        async def text(self):
            return json.dumps({'id': 'job-web-chat-123', 'status': 'running'})

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url, headers=None, ssl=None):
            return FakeResponse()

    monkeypatch.setattr(openai, 'resolve_openai_model_connection', fake_resolve_openai_model_connection)
    monkeypatch.setattr(openai.aiohttp, 'ClientSession', FakeSession)
    monkeypatch.setattr(openai, 'normalize_openclaw_worker_job_payload', lambda payload: payload)

    result = await openai.get_openclaw_worker_job(
        request=object(),
        job_id='job-web-chat-123',
        model='openclaw/main',
        user=object(),
    )

    assert result == {'id': 'job-web-chat-123', 'status': 'running'}


@pytest.mark.asyncio
async def test_maybe_dispatch_openclaw_worker_forces_main_for_multi_agent_prompt(monkeypatch):
    captured_job_payloads = []

    async def fake_fetch_openclaw_worker_json(worker_api_base_url, worker_api_token, method, path, payload=None):
        if path == '/estimate':
            return {
                'selectedAgent': 'visual',
                'recommendedJobType': 'visual_batch',
                'routeReason': ['visual-classified request'],
            }

        if path == '/jobs':
            captured_job_payloads.append(payload)
            return {
                'id': 'job-main-123',
                'agent_id': payload['agent_id'],
                'selected_model_public': 'qwen3.5-9b',
                'estimate': {
                    'selectedAgent': payload['agent_id'],
                    'routeReason': ['forced main orchestration'],
                    'preferredInitialBatch': [payload['agent_id']],
                },
            }

        raise AssertionError(f'unexpected worker path: {path}')

    monkeypatch.setattr(openai, 'fetch_openclaw_worker_json', fake_fetch_openclaw_worker_json)

    result = await openai.maybe_dispatch_openclaw_worker(
        model='openclaw/main',
        payload={
            'messages': [
                {
                    'role': 'user',
                    'content': 'simulate a task that requires multiple agents to collaborate',
                }
            ]
        },
        url='http://127.0.0.1:18789/v1',
        api_config={
            'api_type': 'responses',
            'worker_api_base_url': 'http://127.0.0.1:8090',
        },
        source_channel='openresponses',
    )

    assert result is not None
    assert captured_job_payloads[0]['agent_id'] == 'main'
    assert captured_job_payloads[0]['job_type'] == 'agent_task'


@pytest.mark.asyncio
async def test_maybe_dispatch_openclaw_worker_routes_image_attachment_payload(monkeypatch):
    captured_calls = {}

    class DummyFile:
        filename = 'avatar.png'
        path = '/Users/panda/open-webui/.data/uploads/test-avatar.png'
        meta = {'content_type': 'image/png'}

    async def fake_get_file_by_id(file_id, db=None):
        assert file_id == '3a1c4e65-ad4e-4096-bb39-13fd52917578'
        return DummyFile()

    async def fake_fetch_openclaw_worker_json(worker_api_base_url, worker_api_token, method, path, payload=None):
        if path == '/estimate':
            captured_calls['estimate'] = payload
            return {
                'selectedAgent': 'main',
                'recommendedJobType': 'agent_task',
                'routeReason': ['fallback'],
            }

        if path == '/jobs':
            captured_calls['job'] = payload
            return {
                'id': 'job-visual-123',
                'agent_id': payload['agent_id'],
                'selected_model_public': 'gemma-4-26b-a4b-it',
                'estimate': {
                    'selectedAgent': payload['agent_id'],
                    'routeReason': ['visual attachment forced'],
                    'preferredInitialBatch': [payload['agent_id']],
                },
            }

        raise AssertionError(f'unexpected worker path: {path}')

    monkeypatch.setattr(openai.Files, 'get_file_by_id', fake_get_file_by_id)
    monkeypatch.setattr(openai, 'fetch_openclaw_worker_json', fake_fetch_openclaw_worker_json)

    result = await openai.maybe_dispatch_openclaw_worker(
        model='openclaw/main',
        payload={
            'input': [
                {
                    'type': 'message',
                    'role': 'user',
                    'content': [
                        {'type': 'input_text', 'text': '优化这个图片细节和尺寸。'},
                        {
                            'type': 'input_image',
                            'image_url': '3a1c4e65-ad4e-4096-bb39-13fd52917578',
                        },
                    ],
                }
            ]
        },
        url='http://127.0.0.1:18789/v1',
        api_config={
            'api_type': 'responses',
            'worker_api_base_url': 'http://127.0.0.1:8090',
        },
        source_channel='openresponses',
    )

    assert result is not None
    assert result['handled'] is True
    assert result['job']['id'] == 'job-visual-123'
    assert captured_calls['estimate']['metadata']['attachments'][0]['path'] == '/Users/panda/open-webui/.data/uploads/test-avatar.png'
    assert captured_calls['job']['agent_id'] == 'visual'
    assert captured_calls['job']['job_type'] == 'visual_batch'


@pytest.mark.asyncio
async def test_maybe_dispatch_openclaw_worker_skips_remote_worker_for_path_backed_attachments(monkeypatch):
    class DummyFile:
        filename = 'avatar.png'
        path = '/Users/panda/open-webui/.data/uploads/test-avatar.png'
        meta = {'content_type': 'image/png'}

    async def fake_get_file_by_id(file_id, db=None):
        assert file_id == '3a1c4e65-ad4e-4096-bb39-13fd52917578'
        return DummyFile()

    async def fail_fetch_openclaw_worker_json(*args, **kwargs):
        raise AssertionError('worker endpoint should not be called for remote path-backed attachments')

    monkeypatch.setattr(openai.Files, 'get_file_by_id', fake_get_file_by_id)
    monkeypatch.setattr(openai, 'fetch_openclaw_worker_json', fail_fetch_openclaw_worker_json)

    result = await openai.maybe_dispatch_openclaw_worker(
        model='openclaw/main',
        payload={
            'input': [
                {
                    'type': 'message',
                    'role': 'user',
                    'content': [
                        {'type': 'input_text', 'text': '优化这个图片细节和尺寸。'},
                        {
                            'type': 'input_image',
                            'image_url': '3a1c4e65-ad4e-4096-bb39-13fd52917578',
                        },
                    ],
                }
            ]
        },
        url='http://127.0.0.1:18789/v1',
        api_config={
            'worker_api_base_url': 'http://worker.internal:8090',
        },
        source_channel='openresponses',
    )

    assert result is None
