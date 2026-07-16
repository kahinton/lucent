"""Request review lifecycle extracted from the daemon orchestrator.

Functions accept the live daemon instance and read shared state through the
runtime module, avoiding stale configuration snapshots and circular imports.
"""

from __future__ import annotations

from daemon.runtime.module_proxy import runtime


class RequestReviewMixin:
    """Request-level review behavior composed into the daemon runtime."""

    def _is_request_review_task(self, task: dict):
        return _is_request_review_task(self, task)

    def _parse_review_decision(self, text: str):
        return _parse_review_decision(self, text)

    async def _find_review_agent_type(self, org_id: str, requesting_user_id: str):
        return await _find_review_agent_type(self, org_id, requesting_user_id)

    async def _resolve_review_requesting_user_id(
        self, *, org_id: str, requester_user_id: str
    ):
        return await _resolve_review_requesting_user_id(
            self, org_id=org_id, requester_user_id=requester_user_id
        )

    async def _create_request_review_task(self, request_id: str, request_data: dict):
        return await _create_request_review_task(self, request_id, request_data)

    async def _ensure_request_review_tasks(self):
        return await _ensure_request_review_tasks(self)

    async def _process_request_review_task(self, task: dict, review_result: str):
        return await _process_request_review_task(self, task, review_result)

    async def _handle_review_task_failure(self, task: dict, reason: str):
        return await _handle_review_task_failure(self, task, reason)

    async def _repair_structured_output(
        self,
        task_id: str,
        original_result: str,
        output_contract: dict | None,
        model: str,
    ):
        return await _repair_structured_output(
            self, task_id, original_result, output_contract, model
        )


def _is_request_review_task(daemon, task: dict) -> bool:
    """Identify daemon-created post-completion review tasks.

        Matches the canonical review-task title only. The legacy substring
        check on description ("REQUEST_REVIEW_DECISION:") was removed because
        any unrelated task whose description happened to mention that token
        (docs, code about the review system, prompts) was being misclassified.
        """
    title = (task.get('title') or '').strip().lower()
    return title == runtime.REQUEST_REVIEW_TASK_TITLE.lower()


def _parse_review_decision(daemon, text: str) -> dict:
    """Parse review output into a structured decision.

        Accepted formats:
        - REQUEST_REVIEW_DECISION: APPROVED|NEEDS_REWORK
        - Decision: APPROVED|NEEDS_REWORK
        Plus optional sections:
        - TASK_IDS_TO_REWORK: <id1>, <id2>, ...
        - FEEDBACK: ...
        - MEMORIES_UPDATED: <id1>, <id2>, ... | none
        """
    raw = (text or '').strip()
    upper = raw.upper()
    decision = 'APPROVED' if 'NEEDS_REWORK' not in upper else 'NEEDS_REWORK'
    recognized = False
    m = runtime.re.search('(?:REQUEST_REVIEW_DECISION|DECISION)\\s*:\\s*(APPROVED|NEEDS_REWORK)', raw, flags=runtime.re.IGNORECASE)
    if m:
        decision = m.group(1).upper()
        recognized = True
    elif 'NEEDS_REWORK' in upper:
        decision = 'NEEDS_REWORK'
        recognized = True
    elif 'APPROVED' in upper:
        decision = 'APPROVED'
        recognized = True
    task_ids: list[str] = []
    mt = runtime.re.search('TASK_IDS_TO_REWORK\\s*:\\s*(.+?)(?:\\n[A-Z_ ]+\\s*:|\\Z)', raw, flags=runtime.re.IGNORECASE | runtime.re.DOTALL)
    if mt:
        candidates = runtime.re.split('[\\s,]+', mt.group(1).strip())
        task_ids = [c.strip().strip('[](){}') for c in candidates if c.strip() and runtime.re.fullmatch('[0-9a-fA-F-]{8,64}', c.strip().strip('[](){}'))]
    mf = runtime.re.search('FEEDBACK\\s*:\\s*(.+?)(?:\\n[A-Z_ ]+\\s*:|\\Z)', raw, flags=runtime.re.IGNORECASE | runtime.re.DOTALL)
    feedback = (mf.group(1).strip() if mf else raw)[:10000]
    memories_updated: list[str] = []
    mm = runtime.re.search('MEMORIES_UPDATED\\s*:\\s*(.+?)(?:\\n[A-Z_ ]+\\s*:|\\Z)', raw, flags=runtime.re.IGNORECASE | runtime.re.DOTALL)
    if mm:
        blob = mm.group(1).strip()
        if blob.lower() not in ('none', 'n/a', '-', ''):
            candidates = runtime.re.split('[\\s,]+', blob)
            memories_updated = [c.strip().strip('[](){}') for c in candidates if runtime.re.fullmatch('[0-9a-fA-F-]{8,64}', c.strip().strip('[](){}'))]
    return {'decision': decision, 'task_ids': task_ids, 'feedback': feedback, 'recognized': recognized, 'memories_updated': memories_updated}


async def _find_review_agent_type(daemon, org_id: str, requesting_user_id: str) -> tuple[str | None, str]:
    """Choose request-level review agent type with fallback."""
    primary = runtime.REQUEST_REVIEW_AGENT_TYPE
    fallback = runtime.REQUEST_REVIEW_FALLBACK_AGENT_TYPE
    primary_agent = await runtime.load_accessible_agent(org_id=org_id, requester_user_id=requesting_user_id, agent_type=primary)
    if primary_agent:
        return (primary, 'primary')
    fallback_agent = await runtime.load_accessible_agent(org_id=org_id, requester_user_id=requesting_user_id, agent_type=fallback)
    if fallback_agent:
        runtime.log(f"Request review fallback: using '{fallback}' because '{primary}' is unavailable", 'WARN')
        return (fallback, 'fallback')
    return (None, 'none')


async def _resolve_review_requesting_user_id(daemon, *, org_id: str, requester_user_id: str) -> str:
    """Route daemon-owned review work to a human owner/admin for Handoffs."""
    import asyncpg
    try:
        conn = await asyncpg.connect(runtime.DATABASE_URL)
    except Exception as e:
        runtime.log(f'Review requester resolution DB connect failed: {e}', 'WARN')
        return requester_user_id
    try:
        requester = await conn.fetchrow('SELECT id::text AS id, role, external_id\n                   FROM users\n                   WHERE id = $1::uuid AND organization_id = $2::uuid', requester_user_id, org_id)
        if not requester:
            return requester_user_id
        req_ext = requester['external_id'] or ''
        req_is_daemon = requester['role'] == 'daemon' or req_ext == 'daemon-service' or req_ext.startswith('daemon-service:')
        if not req_is_daemon:
            return requester_user_id
        human = await conn.fetchrow("SELECT id::text AS id\n                   FROM users\n                   WHERE organization_id = $1::uuid\n                     AND is_active = true\n                     AND role IN ('owner', 'admin')\n                     AND COALESCE(external_id, '') <> 'daemon-service'\n                   ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, created_at\n                   LIMIT 1", org_id)
        if human:
            runtime.log(f"Review request owned by daemon user {requester_user_id[:8]}; routing review/Handoffs to human {human['id'][:8]}")
            return str(human['id'])
        return requester_user_id
    except Exception as e:
        runtime.log(f'Review requester resolution failed: {e}', 'WARN')
        return requester_user_id
    finally:
        await conn.close()


async def _create_request_review_task(daemon, request_id: str, request_data: dict) -> dict | None:
    """Create a review task when a request enters review state."""
    tasks = request_data.get('tasks', []) or []
    review_tasks = [t for t in tasks if daemon._is_request_review_task(t)]
    if any((t.get('status') in ('pending', 'planned', 'claimed', 'running') for t in review_tasks)):
        return None
    completed_reviews = [t for t in review_tasks if t.get('status') == 'completed']
    if completed_reviews:
        runtime.log(f'Request {request_id[:8]} still in `review` after {len(completed_reviews)} completed review task(s); auto-finalizing as completed to break loop. (Manually re-open if rework needed.)', 'WARN')
        try:
            await runtime.RequestAPI.update_request_status(request_id, 'completed')
        except Exception as e:
            runtime.log(f'Failed to auto-finalize stuck request {request_id[:8]}: {e}', 'WARN')
        return None
    org_id = str(request_data.get('organization_id', ''))
    requester = request_data.get('created_by')
    if not requester or not org_id:
        runtime.log(f'Request {request_id[:8]} in review but missing created_by/org_id; manual review needed', 'WARN')
        return None
    requesting_user_id = await daemon._resolve_review_requesting_user_id(org_id=org_id, requester_user_id=str(requester))
    agent_type, mode = await daemon._find_review_agent_type(org_id, requesting_user_id)
    if not agent_type:
        runtime.log(f'Request {request_id[:8]} has no accessible review agent; manual review required', 'WARN')
        return None
    non_review_tasks = [t for t in tasks if not daemon._is_request_review_task(t)]
    done_tasks = [t for t in non_review_tasks if t.get('status') in ('completed', 'failed', 'cancelled')]
    task_summaries = []
    task_summary_chars = 0
    review_context_budget = runtime.REQUEST_REVIEW_CONTEXT_CHAR_BUDGET
    for idx, t in enumerate(done_tasks, 1):
        tid = str(t.get('id', ''))
        status = t.get('status', 'unknown')
        title = t.get('title', 'Untitled')
        result_source = t.get('result') or t.get('error') or ''
        result = runtime._truncate_for_context(result_source, runtime.REQUEST_REVIEW_TASK_RESULT_CHAR_BUDGET, label='review task result')
        if not result:
            result = '(no output)'
        outputs = t.get('outputs') or []
        if outputs:
            output_lines = []
            for output in outputs[:10]:
                output_lines.append(f"     - [{output.get('output_type', 'link')}] {output.get('title', 'Untitled output')} url={output.get('url') or 'n/a'} external_id={output.get('external_id') or 'n/a'}")
            outputs_text = '\n   recorded outputs:\n' + '\n'.join(output_lines)
        else:
            outputs_text = '\n   recorded outputs: (none)'
        summary_text = f'{idx}. [{status}] {title}\n   task_id: {tid}\n   output:\n{result}{outputs_text}'
        if task_summary_chars + len(summary_text) > review_context_budget:
            remaining = review_context_budget - task_summary_chars
            if remaining > 10000:
                task_summaries.append(runtime._truncate_for_context(summary_text, remaining, label='request review task summaries'))
            task_summaries.append(f'[Additional task outcomes omitted because request review context exceeded {review_context_budget:,} characters. Reviewers should fetch full task details before approving if the omitted work affects the decision.]')
            break
        task_summaries.append(summary_text)
        task_summary_chars += len(summary_text)
    if not task_summaries:
        task_summaries.append('No terminal tasks found.')
    dep_policy = request_data.get('dependency_policy', 'strict')
    failed_count = sum((1 for t in non_review_tasks if t.get('status') == 'failed'))
    cancelled_count = sum((1 for t in non_review_tasks if t.get('status') == 'cancelled'))
    incomplete_note = ''
    if dep_policy == 'permissive' and (failed_count > 0 or cancelled_count > 0):
        incomplete_note = '\n\nNOTE: dependency_policy is permissive and some tasks are incomplete/failed. Account for this in your review and require rework if needed.'
    target_repo = request_data.get('target_repo') or ''
    target_paths = request_data.get('target_paths') or []
    target_section = ''
    if target_repo or target_paths:
        target_section = f"\n\nTarget persistence scope:\n- target_repo: {target_repo or 'unspecified'}\n- target_paths: {target_paths or []}\nIf this request produced docs/files/reports intended for the target repository, do not approve unless the task outputs show actual durable repo changes (paths plus commit/URL or equivalent recorded artifact)."
    linked_memories = await runtime.RequestAPI.get_request_memories(request_id)
    memory_section = ''
    if linked_memories:
        mem_lines = []
        mem_id_list = []
        for mem in linked_memories:
            mem_id_list.append(str(mem['memory_id']))
            mem_lines.append(f"- Memory ID: {mem['memory_id']}, relation: {mem['relation']}, type: {mem.get('memory_type', 'unknown')}\n  Content: {mem.get('content', '')[:200]}\n  Status: {mem.get('status', 'unknown')}")
        memory_section = '\n\nLinked Memories (MANDATORY UPDATE TARGETS):\n' + '\n'.join(mem_lines) + "\n\n=== MANDATORY MEMORY UPDATE STEP — DO NOT SKIP ===\nBefore you emit REQUEST_REVIEW_DECISION below, you MUST call `update_memory` on EVERY memory listed above. This is a hard precondition for emitting any decision — not a suggestion, not a best practice, not 'if relevant'. The memory update IS part of the review work. A review that skips it is incomplete and will be rejected by the daemon.\n\nRequired calls (one per linked memory):\n" + '\n'.join((f'  - update_memory(memory_id="{mid}", ...)' for mid in mem_id_list)) + '\n\nWhat to update:\n- For \'goal\' memories: append a `progress_notes` entry describing what was accomplished. If a milestone was completed, set that milestone\'s `status` to "completed" and `completed_at` to today. Set the overall goal `status` to "completed" ONLY if every milestone is done.\n- For other memory types: update with any relevant new information from the task results, or call update_memory with a no-op note explaining why no substantive change was needed.\n\nAfter calling update_memory for each linked memory, also call `link_task_memory` to attach any NEW memories created by tasks back to this request.\n\nIn the FEEDBACK section of your decision block below, you MUST include a line of the form:\n  MEMORIES_UPDATED: <comma-separated memory IDs you called update_memory on>\nIf this line is missing or doesn\'t list every linked memory ID, the daemon will treat the review as incomplete and re-queue it.\n=== END MANDATORY STEP ===\n'
    review_description = f"""Perform post-completion request review.\n\nYou are validating whether the request outcomes satisfy the original request goals AND propagating those outcomes into linked memories.\n\nOriginal request title: {request_data.get('title', '')}\nOriginal request description:\n{request_data.get('description', '')}\n\n{target_section}\n\nTask outcomes:\n{chr(10).join(task_summaries)}{incomplete_note}{memory_section}\n\n=== OUTPUT ARTIFACT REVIEW ===\nEach task may include a 'recorded outputs' list. These are the user-visible deliverables shown in the Activity UI: GitHub PRs/issues, emails, docs, files, deployments, memories, or generic artifacts.\nDurable persistence is mandatory for external artifacts: if the request asked for repository documentation/files or named a target_repo, narrative markdown in the task result is insufficient. Require concrete changed file paths and a commit/URL (or an explicit BLOCKED result explaining missing write capability).\nBefore approving, verify that every deliverable mentioned in task output has a corresponding recorded output. Plain URLs in task results are auto-extracted by Lucent, but non-URL deliverables such as sent emails, created documents identified only by provider ID, or files stored in an external system may require an explicit `record_task_output` call.\nIf a deliverable is missing and you have enough task_id/title/url or external_id/provider information, call `record_task_output` before approving. If you cannot identify the missing deliverable precisely, return NEEDS_REWORK and ask the task agent to record the output.\n=== END OUTPUT ARTIFACT REVIEW ===\n\n=== SETUP/CONFIGURATION BLOCKER HANDOFFS ===\nBefore deciding APPROVED or NEEDS_REWORK, check whether task output reports a legitimate environment, setup, credential, permission, dependency, or configuration blocker that prevented useful completion. Examples include missing API keys, inaccessible services, invalid local configuration, unavailable MCP servers, sandbox provisioning failures, missing repo permissions, or dependency installation failures that the task agent could not reasonably fix.\nWhen a task was blocked by a user- or environment-actionable issue and reported what was attempted clearly, call `send_handoff` before emitting REQUEST_REVIEW_DECISION. The handoff must explain what was attempted, what blocked progress, why Lucent could not resolve it autonomously, and exactly what the user may need to configure or verify next. Include request/task references when IDs are available, set `requires_response=true` only if Lucent needs an answer before continuing, and use a stable `dedupe_key` like `review-blocker:<request-id>:<task-id-or-topic>`. A narrative-only handoff section in your final output is not enough; you must call `send_handoff` so the user sees a Handoffs item.\nApprove with a blocker handoff only when the blocker is external and the task's report is clear enough for the user to act on. Return NEEDS_REWORK when the task merely blames setup/configuration without evidence, attempted actions, or actionable remediation detail.\n=== END SETUP/CONFIGURATION BLOCKER HANDOFFS ===\n\nReturn your decision in this exact machine-readable shape (emit it ONLY after completing all mandatory memory updates above):\nREQUEST_REVIEW_DECISION: APPROVED|NEEDS_REWORK\nTASK_IDS_TO_REWORK: <comma-separated task ids, optional when approved>\nFEEDBACK: <actionable rationale and correction guidance>\nMEMORIES_UPDATED: <comma-separated memory IDs you called update_memory on; use "none" only when no Linked Memories section was provided>\nHANDOFF_SENT: <handoff URL/id if you called send_handoff, or "none">"""
    review_model, review_model_reason = runtime._select_model_for_task(agent_type=agent_type, title=runtime.REQUEST_REVIEW_TASK_TITLE, description=review_description, explicit_model=runtime.REQUEST_REVIEW_MODEL or None)
    review_task = await runtime.RequestAPI.create_task(request_id=request_id, title=runtime.REQUEST_REVIEW_TASK_TITLE, agent_type=agent_type, description=review_description, priority=request_data.get('priority', 'medium'), sequence_order=10000000, model=review_model, requesting_user_id=requesting_user_id)
    if review_task:
        runtime.log(f"Request {request_id[:8]} moved to review; created review task {str(review_task.get('id', ''))[:8]} agent={agent_type} mode={mode} model={review_model} ({review_model_reason})")
    else:
        runtime.log(f'Failed to create review task for request {request_id[:8]}; manual review required', 'WARN')
    return review_task


async def _ensure_request_review_tasks(self) -> None:
    """Ensure each request in review has a queued review task."""
    review_requests = await runtime.RequestAPI.list_requests_in_review()
    if not review_requests:
        return
    for req in review_requests:
        req_id = str(req.get('id', ''))
        status = req.get('status')
        if not req_id or status != 'review':
            continue
        full = await runtime.RequestAPI.get_request(req_id)
        if not full:
            continue
        created = await self._create_request_review_task(req_id, full)
        if created:
            await runtime.RequestAPI.add_event(str(created.get('id')), 'request_review_started', f'Auto-created review task for request {req_id[:8]}')


async def _process_request_review_task(daemon, task: dict, review_result: str) -> None:
    """Process the internal review decision and finalize the request.

        The internal review is a self-check: did the work accomplish what was
        requested?  APPROVED → auto-complete the request.  NEEDS_REWORK →
        transition to needs_rework so the daemon retries.

        Human review (the review queue UI) is a separate concept — it is only
        for autonomous actions that require human sign-off, not for the
        standard post-completion quality check handled here.
        """
    request_id = str(task.get('request_id', ''))
    if not request_id:
        return
    request_data = await runtime.RequestAPI.get_request(request_id)
    if not request_data:
        return
    parsed = daemon._parse_review_decision(review_result or '')
    decision = parsed['decision']
    feedback = parsed['feedback']
    task_ids = parsed['task_ids']
    recognized = bool(parsed.get('recognized'))
    if not recognized:
        runtime.log(f'Request review output for {request_id[:8]} not parseable; auto-completing', 'WARN')
        await runtime.RequestAPI.add_event(str(task['id']), 'request_review_parse_error', 'Could not parse review decision; auto-completing request.', {'recommendation': 'UNPARSEABLE', 'feedback': feedback[:1000]})
        await runtime.RequestAPI.update_request_status(request_id, 'completed')
        return
    if decision == 'APPROVED':
        linked_memories = await runtime.RequestAPI.get_request_memories(request_id)
        if linked_memories:
            expected_ids = {str(m['memory_id']) for m in linked_memories}
            attested_ids = {mid.lower() for mid in parsed.get('memories_updated', [])}
            missing = sorted((eid for eid in expected_ids if eid.lower() not in attested_ids))
            if missing:
                await runtime.RequestAPI.add_event(str(task['id']), 'request_review_memory_update_missing', f"Review approved without attesting to required memory updates. Missing MEMORIES_UPDATED entries for: {', '.join((m[:8] for m in missing))}.", {'expected_memory_ids': sorted(expected_ids), 'attested_memory_ids': sorted(attested_ids), 'missing_memory_ids': missing})
                await runtime.RequestAPI.update_request_status(request_id, 'needs_rework')
                runtime.log(f'Request {request_id[:8]} review APPROVED but missing memory updates for {len(missing)} memory/memories; sent back for rework', 'WARN')
                return
        await runtime.RequestAPI.add_event(str(task['id']), 'request_review_approved', 'Internal review: APPROVED. Auto-completing request.', {'recommendation': decision, 'feedback': feedback[:1500], 'memories_updated': parsed.get('memories_updated', [])})
        await runtime.RequestAPI.update_request_status(request_id, 'completed')
        runtime.log(f'Request {request_id[:8]} internal review APPROVED — completed')
        return
    await runtime.RequestAPI.add_event(str(task['id']), 'request_review_needs_rework', 'Internal review: NEEDS_REWORK. Sending back for revision.', {'recommendation': decision, 'feedback': feedback[:1500], 'task_ids_to_rework': task_ids})
    await runtime.RequestAPI.update_request_status(request_id, 'needs_rework')
    runtime.log(f'Request {request_id[:8]} internal review NEEDS_REWORK — sent back for revision')


async def _handle_review_task_failure(daemon, task: dict, reason: str) -> None:
    """Do not hard-fail a request when the review task itself fails.

        Complete the review task with a manual-review marker and move request to needs_rework.
        """
    task_id = str(task.get('id', ''))
    request_id = str(task.get('request_id', ''))
    note = f'Automatic request review failed; manual review required.\n\nReason: {reason}'
    try:
        await runtime.RequestAPI.complete_task(task_id, note, instance_id=daemon.instance_id)
    except TypeError:
        await runtime.RequestAPI.complete_task(task_id, note)
    if request_id:
        await runtime.RequestAPI.update_request_status(request_id, 'needs_rework')
    await runtime.RequestAPI.add_event(task_id, 'request_review_manual_required', note[:1500])
    runtime.log(f'Review task {task_id[:8]} failed non-fatally; request {request_id[:8]} marked needs_rework', 'WARN')


async def _repair_structured_output(daemon, task_id: str, original_result: str, output_contract: dict | None, model: str) -> str | None:
    """Ask a model to reformat output to match the task's JSON Schema contract."""
    if not output_contract:
        return None
    schema = output_contract.get('json_schema', {})
    schema_str = runtime.json.dumps(schema, indent=2)
    repair_prompt = f"The following agent response was supposed to include structured output matching a JSON Schema, but validation failed.\n\nSchema:\n```json\n{schema_str}\n```\n\nOriginal response (first 10000 chars):\n{(original_result or '')[:10000]}\n\nExtract relevant data from the response and produce a valid JSON object matching the schema. Wrap it in <task_output> tags.\n\n<task_output>\n{{...your JSON here...}}\n</task_output>"
    try:
        return await daemon.run_session(f'repair-{task_id[:8]}', 'You are a data extraction assistant. Extract structured data from text and format it as JSON matching the given schema. Output ONLY the <task_output> block.', repair_prompt, model=model)
    except Exception as exc:
        runtime.log(f'Repair session failed for {task_id[:8]}: {exc}', 'WARN')
        return None
