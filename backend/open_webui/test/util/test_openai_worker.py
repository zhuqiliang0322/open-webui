import json

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
