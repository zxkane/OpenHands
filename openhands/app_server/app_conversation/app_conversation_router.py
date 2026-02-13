"""Sandboxed Conversation router for OpenHands App Server."""

import asyncio
import logging
import os
import sys
import tempfile
from datetime import datetime
from typing import Annotated, AsyncGenerator, Literal
from uuid import UUID

import httpx

from openhands.app_server.services.db_session_injector import set_db_session_keep_open
from openhands.app_server.services.httpx_client_injector import (
    set_httpx_client_keep_open,
)
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.user.specifiy_user_context import USER_CONTEXT_ATTR
from openhands.app_server.user.user_context import UserContext

# Handle anext compatibility for Python < 3.10
if sys.version_info >= (3, 10):
    from builtins import anext
else:

    async def anext(async_iterator):
        """Compatibility function for anext in Python < 3.10"""
        return await async_iterator.__anext__()


from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.app_conversation.app_conversation_models import (
    AppConversation,
    AppConversationPage,
    AppConversationStartRequest,
    AppConversationStartTask,
    AppConversationStartTaskPage,
    AppConversationStartTaskSortOrder,
    AppConversationUpdateRequest,
    SkillResponse,
)
from openhands.app_server.app_conversation.app_conversation_service import (
    AppConversationService,
)
from openhands.app_server.app_conversation.app_conversation_service_base import (
    AppConversationServiceBase,
)
from openhands.app_server.app_conversation.app_conversation_start_task_service import (
    AppConversationStartTaskService,
)
from openhands.app_server.config import (
    depends_app_conversation_service,
    depends_app_conversation_start_task_service,
    depends_db_session,
    depends_httpx_client,
    depends_sandbox_service,
    depends_sandbox_spec_service,
    depends_user_context,
    get_app_conversation_service,
)
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_service import SandboxService
from openhands.app_server.sandbox.sandbox_spec_service import SandboxSpecService
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
)
from openhands.sdk.context.skills import KeywordTrigger, TaskTrigger
from openhands.sdk.workspace.remote.async_remote_workspace import AsyncRemoteWorkspace

router = APIRouter(prefix='/app-conversations', tags=['Conversations'])
logger = logging.getLogger(__name__)
app_conversation_service_dependency = depends_app_conversation_service()
app_conversation_start_task_service_dependency = (
    depends_app_conversation_start_task_service()
)
user_context_dependency = depends_user_context()
db_session_dependency = depends_db_session()
httpx_client_dependency = depends_httpx_client()
sandbox_service_dependency = depends_sandbox_service()
sandbox_spec_service_dependency = depends_sandbox_spec_service()

# Read methods


@router.get('/search')
async def search_app_conversations(
    title__contains: Annotated[
        str | None,
        Query(title='Filter by title containing this string'),
    ] = None,
    created_at__gte: Annotated[
        datetime | None,
        Query(title='Filter by created_at greater than or equal to this datetime'),
    ] = None,
    created_at__lt: Annotated[
        datetime | None,
        Query(title='Filter by created_at less than this datetime'),
    ] = None,
    updated_at__gte: Annotated[
        datetime | None,
        Query(title='Filter by updated_at greater than or equal to this datetime'),
    ] = None,
    updated_at__lt: Annotated[
        datetime | None,
        Query(title='Filter by updated_at less than this datetime'),
    ] = None,
    page_id: Annotated[
        str | None,
        Query(title='Optional next_page_id from the previously returned page'),
    ] = None,
    limit: Annotated[
        int,
        Query(
            title='The max number of results in the page',
            gt=0,
            lte=100,
        ),
    ] = 100,
    include_sub_conversations: Annotated[
        bool,
        Query(
            title='If True, include sub-conversations in the results. If False (default), exclude all sub-conversations.'
        ),
    ] = False,
    app_conversation_service: AppConversationService = (
        app_conversation_service_dependency
    ),
) -> AppConversationPage:
    """Search / List sandboxed conversations."""
    assert limit > 0
    assert limit <= 100
    return await app_conversation_service.search_app_conversations(
        title__contains=title__contains,
        created_at__gte=created_at__gte,
        created_at__lt=created_at__lt,
        updated_at__gte=updated_at__gte,
        updated_at__lt=updated_at__lt,
        page_id=page_id,
        limit=limit,
        include_sub_conversations=include_sub_conversations,
    )


@router.get('/count')
async def count_app_conversations(
    title__contains: Annotated[
        str | None,
        Query(title='Filter by title containing this string'),
    ] = None,
    created_at__gte: Annotated[
        datetime | None,
        Query(title='Filter by created_at greater than or equal to this datetime'),
    ] = None,
    created_at__lt: Annotated[
        datetime | None,
        Query(title='Filter by created_at less than this datetime'),
    ] = None,
    updated_at__gte: Annotated[
        datetime | None,
        Query(title='Filter by updated_at greater than or equal to this datetime'),
    ] = None,
    updated_at__lt: Annotated[
        datetime | None,
        Query(title='Filter by updated_at less than this datetime'),
    ] = None,
    app_conversation_service: AppConversationService = (
        app_conversation_service_dependency
    ),
) -> int:
    """Count sandboxed conversations matching the given filters."""
    return await app_conversation_service.count_app_conversations(
        title__contains=title__contains,
        created_at__gte=created_at__gte,
        created_at__lt=created_at__lt,
        updated_at__gte=updated_at__gte,
        updated_at__lt=updated_at__lt,
    )


@router.get('')
async def batch_get_app_conversations(
    ids: Annotated[list[str], Query()],
    app_conversation_service: AppConversationService = (
        app_conversation_service_dependency
    ),
) -> list[AppConversation | None]:
    """Get a batch of sandboxed conversations given their ids. Return None for any missing.

    Accepts UUIDs as strings (with or without dashes) and converts them internally.
    Returns 400 Bad Request if any string cannot be converted to a valid UUID.
    """
    if len(ids) >= 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Too many ids requested. Maximum is 99.',
        )

    uuids: list[UUID] = []
    invalid_ids: list[str] = []
    for id_str in ids:
        try:
            uuids.append(UUID(id_str))
        except ValueError:
            invalid_ids.append(id_str)

    if invalid_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid UUID format for ids: {invalid_ids}',
        )

    app_conversations = await app_conversation_service.batch_get_app_conversations(
        uuids
    )
    return app_conversations


@router.post('')
async def start_app_conversation(
    request: Request,
    start_request: AppConversationStartRequest,
    db_session: AsyncSession = db_session_dependency,
    httpx_client: httpx.AsyncClient = httpx_client_dependency,
    app_conversation_service: AppConversationService = (
        app_conversation_service_dependency
    ),
) -> AppConversationStartTask:
    # Because we are processing after the request finishes, keep the db connection open
    set_db_session_keep_open(request.state, True)
    set_httpx_client_keep_open(request.state, True)

    try:
        """Start an app conversation start task and return it."""
        async_iter = app_conversation_service.start_app_conversation(start_request)
        result = await anext(async_iter)
        asyncio.create_task(_consume_remaining(async_iter, db_session, httpx_client))
        return result
    except Exception:
        await db_session.close()
        await httpx_client.aclose()
        raise


@router.post('/{conversation_id}/resume')
async def resume_app_conversation(
    conversation_id: str,
    request: Request,
    response: Response,
    app_conversation_service: AppConversationService = app_conversation_service_dependency,
) -> dict:
    '''Resume an ARCHIVED conversation by recreating its sandbox.

    This endpoint triggers sandbox recreation for conversations that were archived
    due to EC2 instance replacement. The sandbox will be recreated with the same
    conversation_id and user_id labels, allowing the workspace to be reconnected.
    '''
    import logging
    from uuid import UUID
    _logger = logging.getLogger(__name__)

    conv_uuid = UUID(conversation_id)

    # Get the conversation info to check status and get user_id
    conversations = await app_conversation_service.batch_get_app_conversations([conv_uuid])
    if not conversations or not conversations[0]:
        response.status_code = 404
        return {"error": "Conversation not found"}

    conv = conversations[0]

    # Only resume ARCHIVED conversations (sandbox is MISSING)
    from openhands.app_server.sandbox.sandbox_models import SandboxStatus
    if conv.sandbox_status != SandboxStatus.MISSING:
        return {"status": "ok", "message": "Conversation is not archived", "sandbox_status": str(conv.sandbox_status)}

    _logger.info(f"Resume requested for ARCHIVED conversation: {conversation_id}")

    # Get the user_id from the conversation info service
    conv_info = await app_conversation_service.app_conversation_info_service.get_app_conversation_info(conv_uuid)
    if not conv_info:
        response.status_code = 404
        return {"error": "Conversation info not found"}

    user_id = conv_info.created_by_user_id
    sandbox_id = conv_info.sandbox_id

    if not sandbox_id:
        response.status_code = 400
        return {"error": "No sandbox_id found for conversation"}

    _logger.info(f"Recreating sandbox {sandbox_id} for user {user_id}")

    try:
        # Strip the oh-agent-server- prefix if present
        sandbox_id_for_start = sandbox_id
        prefix = 'oh-agent-server-'
        if sandbox_id_for_start.startswith(prefix):
            sandbox_id_for_start = sandbox_id_for_start[len(prefix):]

        # Directly call sandbox_service.start_sandbox with user_id
        sandbox = await app_conversation_service.sandbox_service.start_sandbox(
            sandbox_id=sandbox_id_for_start,
            user_id=user_id,
        )

        _logger.info(f"Sandbox recreated for conversation {conversation_id}: {sandbox.id}")

        # Patch 29: Re-inject secrets into recreated sandbox
        # Resumed conversations have masked secrets in persisted state.
        # We re-inject fresh secrets via the sandbox's POST /api/conversations/{conv_id}/secrets endpoint.
        try:
            import asyncio as _asyncio

            async def _reinject_secrets():
                """Background task to re-inject secrets after sandbox is ready."""
                import httpx
                import docker
                _sec_logger = logging.getLogger(__name__)

                _conv_id_hex = conversation_id.replace('-', '')
                _container_name = f'oh-agent-server-{_conv_id_hex}'
                _sandbox_port = None
                _api_key = None

                try:
                    _docker_client = docker.from_env()
                    _container = _docker_client.containers.get(_container_name)
                    _ports = _container.attrs.get('NetworkSettings', {}).get('Ports', {})
                    _port_bindings = _ports.get('8000/tcp', [])
                    if _port_bindings:
                        _sandbox_port = _port_bindings[0].get('HostPort')
                    _sec_logger.info(f"Patch 29: Found sandbox container {_container_name} on port {_sandbox_port}")
                except Exception as _docker_err:
                    _sec_logger.warning(f"Patch 29: Docker lookup failed: {_docker_err}")
                    return

                if not _sandbox_port:
                    _sec_logger.warning(f"Patch 29: No port mapping found for {_container_name}")
                    return

                _sandbox_url = f'http://host.docker.internal:{_sandbox_port}'

                _healthy = False
                async with httpx.AsyncClient(timeout=10) as _health_client:
                    for _attempt in range(36):
                        try:
                            _health_resp = await _health_client.get(f'{_sandbox_url}/health')
                            if _health_resp.status_code == 200:
                                _healthy = True
                                _sec_logger.info(f"Patch 29: Sandbox healthy after {(_attempt + 1) * 5}s")
                                break
                        except Exception:
                            pass
                        await _asyncio.sleep(5)

                if not _healthy:
                    _sec_logger.warning(f"Patch 29: Sandbox not healthy after 180s, skipping secret re-injection")
                    return

                try:
                    _env_list = _container.attrs.get('Config', {}).get('Env', [])
                    for _env_item in _env_list:
                        if _env_item.startswith('OH_SESSION_API_KEYS_0='):
                            _api_key = _env_item.split('=', 1)[1]
                            break
                except Exception as _key_err:
                    _sec_logger.warning(f"Patch 29: Failed to get API key from env: {_key_err}")

                _user = await app_conversation_service.user_context.get_user_info()
                _secrets = await app_conversation_service._setup_secrets_for_git_providers(_user)
                _user_secrets = await app_conversation_service.user_context.get_secrets()
                _secrets.update(_user_secrets)

                if not _secrets:
                    _sec_logger.info("Patch 29: No secrets to inject for this user")
                    return

                _secret_data = {}
                for _name, _secret_val in _secrets.items():
                    _secret_data[_name] = _secret_val.model_dump(mode='json', context={'expose_secrets': True})

                _headers = {}
                if _api_key:
                    _headers['X-Session-API-Key'] = _api_key
                async with httpx.AsyncClient(timeout=30) as _client:
                    _resp = await _client.post(
                        f'{_sandbox_url}/api/conversations/{_conv_id_hex}/secrets',
                        json={'secrets': _secret_data},
                        headers=_headers,
                    )
                    if _resp.status_code in (200, 201, 204):
                        _sec_logger.info(f"Patch 29: Re-injected {len(_secrets)} secrets into resumed sandbox {conversation_id}")
                    else:
                        _sec_logger.warning(f"Patch 29: Secret injection returned {_resp.status_code}: {_resp.text[:200]}")

            _asyncio.create_task(_reinject_secrets())

        except Exception as _sec_err:
            _logger.warning(f"Patch 29: Secret re-injection setup failed (non-fatal): {_sec_err}")

        return {"status": "ok", "sandbox_id": sandbox.id}
    except Exception as e:
        _logger.exception(f"Failed to resume conversation {conversation_id}: {e}")
        response.status_code = 500
        return {"error": str(e)}


@router.patch('/{conversation_id}')
async def update_app_conversation(
    conversation_id: str,
    update_request: AppConversationUpdateRequest,
    app_conversation_service: AppConversationService = (
        app_conversation_service_dependency
    ),
) -> AppConversation:
    info = await app_conversation_service.update_app_conversation(
        UUID(conversation_id), update_request
    )
    if info is None:
        raise HTTPException(404, 'unknown_app_conversation')
    return info


@router.post('/stream-start')
async def stream_app_conversation_start(
    request: AppConversationStartRequest,
    user_context: UserContext = user_context_dependency,
) -> list[AppConversationStartTask]:
    """Start an app conversation start task and stream updates from it.
    Leaves the connection open until either the conversation starts or there was an error
    """
    response = StreamingResponse(
        _stream_app_conversation_start(request, user_context),
        media_type='application/json',
    )
    return response


@router.get('/start-tasks/search')
async def search_app_conversation_start_tasks(
    conversation_id__eq: Annotated[
        UUID | None,
        Query(title='Filter by conversation ID equal to this value'),
    ] = None,
    created_at__gte: Annotated[
        datetime | None,
        Query(title='Filter by created_at greater than or equal to this datetime'),
    ] = None,
    sort_order: Annotated[
        AppConversationStartTaskSortOrder,
        Query(title='Sort order for the results'),
    ] = AppConversationStartTaskSortOrder.CREATED_AT_DESC,
    page_id: Annotated[
        str | None,
        Query(title='Optional next_page_id from the previously returned page'),
    ] = None,
    limit: Annotated[
        int,
        Query(
            title='The max number of results in the page',
            gt=0,
            lte=100,
        ),
    ] = 100,
    app_conversation_start_task_service: AppConversationStartTaskService = (
        app_conversation_start_task_service_dependency
    ),
) -> AppConversationStartTaskPage:
    """Search / List conversation start tasks."""
    assert limit > 0
    assert limit <= 100
    return (
        await app_conversation_start_task_service.search_app_conversation_start_tasks(
            conversation_id__eq=conversation_id__eq,
            created_at__gte=created_at__gte,
            sort_order=sort_order,
            page_id=page_id,
            limit=limit,
        )
    )


@router.get('/start-tasks/count')
async def count_app_conversation_start_tasks(
    conversation_id__eq: Annotated[
        UUID | None,
        Query(title='Filter by conversation ID equal to this value'),
    ] = None,
    created_at__gte: Annotated[
        datetime | None,
        Query(title='Filter by created_at greater than or equal to this datetime'),
    ] = None,
    app_conversation_start_task_service: AppConversationStartTaskService = (
        app_conversation_start_task_service_dependency
    ),
) -> int:
    """Count conversation start tasks matching the given filters."""
    return await app_conversation_start_task_service.count_app_conversation_start_tasks(
        conversation_id__eq=conversation_id__eq,
        created_at__gte=created_at__gte,
    )


@router.get('/start-tasks')
async def batch_get_app_conversation_start_tasks(
    ids: Annotated[list[UUID], Query()],
    app_conversation_start_task_service: AppConversationStartTaskService = (
        app_conversation_start_task_service_dependency
    ),
) -> list[AppConversationStartTask | None]:
    """Get a batch of start app conversation tasks given their ids. Return None for any missing."""
    assert len(ids) < 100
    start_tasks = await app_conversation_start_task_service.batch_get_app_conversation_start_tasks(
        ids
    )
    return start_tasks


@router.get('/{conversation_id}/file')
async def read_conversation_file(
    conversation_id: UUID,
    file_path: Annotated[
        str,
        Query(title='Path to the file to read within the sandbox workspace'),
    ] = '/workspace/project/PLAN.md',
    app_conversation_service: AppConversationService = (
        app_conversation_service_dependency
    ),
    sandbox_service: SandboxService = sandbox_service_dependency,
    sandbox_spec_service: SandboxSpecService = sandbox_spec_service_dependency,
) -> str:
    """Read a file from a specific conversation's sandbox workspace.

    Returns the content of the file at the specified path if it exists, otherwise returns an empty string.

    Args:
        conversation_id: The UUID of the conversation
        file_path: Path to the file to read within the sandbox workspace

    Returns:
        The content of the file or an empty string if the file doesn't exist
    """
    # Get the conversation info
    conversation = await app_conversation_service.get_app_conversation(conversation_id)
    if not conversation:
        return ''

    # Get the sandbox info
    sandbox = await sandbox_service.get_sandbox(conversation.sandbox_id)
    if not sandbox or sandbox.status != SandboxStatus.RUNNING:
        return ''

    # Get the sandbox spec to find the working directory
    sandbox_spec = await sandbox_spec_service.get_sandbox_spec(sandbox.sandbox_spec_id)
    if not sandbox_spec:
        return ''

    # Get the agent server URL
    if not sandbox.exposed_urls:
        return ''

    agent_server_url = None
    for exposed_url in sandbox.exposed_urls:
        if exposed_url.name == AGENT_SERVER:
            agent_server_url = exposed_url.url
            break

    if not agent_server_url:
        return ''

    agent_server_url = replace_localhost_hostname_for_docker(agent_server_url)

    # Create remote workspace
    remote_workspace = AsyncRemoteWorkspace(
        host=agent_server_url,
        api_key=sandbox.session_api_key,
        working_dir=sandbox_spec.working_dir,
    )

    # Read the file at the specified path
    temp_file_path = None
    try:
        # Create a temporary file path to download the remote file
        with tempfile.NamedTemporaryFile(mode='w+b', delete=False) as temp_file:
            temp_file_path = temp_file.name

        # Download the file from remote system
        result = await remote_workspace.file_download(
            source_path=file_path,
            destination_path=temp_file_path,
        )

        if result.success:
            # Read the content from the temporary file
            with open(temp_file_path, 'rb') as f:
                content = f.read()
            # Decode bytes to string
            return content.decode('utf-8')
    except Exception:
        # If there's any error reading the file, return empty string
        pass
    finally:
        # Clean up the temporary file
        if temp_file_path:
            try:
                os.unlink(temp_file_path)
            except Exception:
                # Ignore errors during cleanup
                pass

    return ''


@router.get('/{conversation_id}/skills')
async def get_conversation_skills(
    conversation_id: UUID,
    app_conversation_service: AppConversationService = (
        app_conversation_service_dependency
    ),
    sandbox_service: SandboxService = sandbox_service_dependency,
    sandbox_spec_service: SandboxSpecService = sandbox_spec_service_dependency,
) -> JSONResponse:
    """Get all skills associated with the conversation.

    This endpoint returns all skills that are loaded for the v1 conversation.
    Skills are loaded from multiple sources:
    - Sandbox skills (exposed URLs)
    - Global skills (OpenHands/skills/)
    - User skills (~/.openhands/skills/)
    - Organization skills (org/.openhands repository)
    - Repository skills (repo/.openhands/skills/ or .openhands/microagents/)

    Returns:
        JSONResponse: A JSON response containing the list of skills.
    """
    try:
        # Get the conversation info
        conversation = await app_conversation_service.get_app_conversation(
            conversation_id
        )
        if not conversation:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={'error': f'Conversation {conversation_id} not found'},
            )

        # Get the sandbox info
        sandbox = await sandbox_service.get_sandbox(conversation.sandbox_id)
        if not sandbox or sandbox.status != SandboxStatus.RUNNING:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    'error': f'Sandbox not found or not running for conversation {conversation_id}'
                },
            )

        # Get the sandbox spec to find the working directory
        sandbox_spec = await sandbox_spec_service.get_sandbox_spec(
            sandbox.sandbox_spec_id
        )
        if not sandbox_spec:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={'error': 'Sandbox spec not found'},
            )

        # Get the agent server URL
        if not sandbox.exposed_urls:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={'error': 'No agent server URL found for sandbox'},
            )

        agent_server_url = None
        for exposed_url in sandbox.exposed_urls:
            if exposed_url.name == AGENT_SERVER:
                agent_server_url = exposed_url.url
                break

        if not agent_server_url:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={'error': 'Agent server URL not found in sandbox'},
            )

        agent_server_url = replace_localhost_hostname_for_docker(agent_server_url)

        # Load skills from all sources
        logger.info(f'Loading skills for conversation {conversation_id}')

        # Prefer the shared loader to avoid duplication; otherwise return empty list.
        all_skills: list = []
        if isinstance(app_conversation_service, AppConversationServiceBase):
            all_skills = await app_conversation_service.load_and_merge_all_skills(
                sandbox,
                conversation.selected_repository,
                sandbox_spec.working_dir,
                agent_server_url,
            )

        logger.info(
            f'Loaded {len(all_skills)} skills for conversation {conversation_id}: '
            f'{[s.name for s in all_skills]}'
        )

        # Transform skills to response format
        skills_response = []
        for skill in all_skills:
            # Determine type based on AgentSkills format and trigger
            skill_type: Literal['repo', 'knowledge', 'agentskills']
            if skill.is_agentskills_format:
                skill_type = 'agentskills'
            elif skill.trigger is None:
                skill_type = 'repo'
            else:
                skill_type = 'knowledge'

            # Extract triggers
            triggers = []
            if isinstance(skill.trigger, (KeywordTrigger, TaskTrigger)):
                if hasattr(skill.trigger, 'keywords'):
                    triggers = skill.trigger.keywords
                elif hasattr(skill.trigger, 'triggers'):
                    triggers = skill.trigger.triggers

            skills_response.append(
                SkillResponse(
                    name=skill.name,
                    type=skill_type,
                    content=skill.content,
                    triggers=triggers,
                )
            )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={'skills': [s.model_dump() for s in skills_response]},
        )

    except Exception as e:
        logger.error(f'Error getting skills for conversation {conversation_id}: {e}')
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'error': f'Error getting skills: {str(e)}'},
        )


@router.get('/{conversation_id}/download')
async def export_conversation(
    conversation_id: UUID,
    app_conversation_service: AppConversationService = (
        app_conversation_service_dependency
    ),
):
    """Download a conversation trajectory as a zip file.

    Returns a zip file containing all events and metadata for the conversation.

    Args:
        conversation_id: The UUID of the conversation to download

    Returns:
        A zip file containing the conversation trajectory
    """
    try:
        # Get the zip file content
        zip_content = await app_conversation_service.export_conversation(
            conversation_id
        )

        # Return as a downloadable zip file
        return Response(
            content=zip_content,
            media_type='application/zip',
            headers={
                'Content-Disposition': f'attachment; filename="conversation_{conversation_id}.zip"'
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f'Failed to download trajectory: {str(e)}'
        )


async def _consume_remaining(
    async_iter, db_session: AsyncSession, httpx_client: httpx.AsyncClient
):
    """Consume the remaining items from an async iterator"""
    try:
        while True:
            await anext(async_iter)
    except StopAsyncIteration:
        return
    finally:
        await db_session.close()
        await httpx_client.aclose()


async def _stream_app_conversation_start(
    request: AppConversationStartRequest,
    user_context: UserContext,
) -> AsyncGenerator[str, None]:
    """Stream a json list, item by item."""
    # Because the original dependencies are closed after the method returns, we need
    # a new dependency context which will continue intil the stream finishes.
    state = InjectorState()
    setattr(state, USER_CONTEXT_ATTR, user_context)
    async with get_app_conversation_service(state) as app_conversation_service:
        yield '[\n'
        comma = False
        async for task in app_conversation_service.start_app_conversation(request):
            chunk = task.model_dump_json()
            if comma:
                chunk = ',\n' + chunk
            comma = True
            yield chunk
        yield ']'
