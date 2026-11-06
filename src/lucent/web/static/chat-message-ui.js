(function() {
    function escapeHtml(value) {
        if (!value) return '';
        const element = document.createElement('div');
        element.textContent = value;
        return element.innerHTML;
    }

    function formatJSON(value) {
        if (!value) return '';
        if (typeof value === 'object') return JSON.stringify(value, null, 2);
        try {
            return JSON.stringify(JSON.parse(value), null, 2);
        } catch (_error) {
            return String(value);
        }
    }

    function renderMarkdown(value) {
        let text = String(value || '');
        if (!text) return '';

        text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, language, code) => {
            const languageLabel = language
                ? `<span class="absolute top-2 right-2 text-[10px] text-gray-500 font-sans">${escapeHtml(language)}</span>`
                : '';
            return `<div class="relative my-3"><pre class="bg-gray-900 text-gray-100 rounded-xl p-4 overflow-x-auto text-xs leading-relaxed">${languageLabel}<code>${escapeHtml(code.trim())}</code></pre></div>`;
        });
        text = text.replace(/`([^`]+)`/g, (_, code) =>
            `<code class="bg-gray-100 text-primary-700 px-1.5 py-0.5 rounded-md text-xs font-medium">${escapeHtml(code)}</code>`
        );
        text = text.replace(/\*\*([^*]+)\*\*/g, '<strong class="font-semibold text-gray-900">$1</strong>');
        text = text.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');
        text = text.replace(/^### (.+)$/gm, '<h4 class="font-semibold text-gray-900 mt-3 mb-1 text-sm">$1</h4>');
        text = text.replace(/^## (.+)$/gm, '<h3 class="font-semibold text-gray-900 mt-4 mb-1.5">$1</h3>');
        text = text.replace(/^# (.+)$/gm, '<h2 class="font-bold text-gray-900 mt-4 mb-2 text-lg">$1</h2>');
        text = text.replace(/^---$/gm, '<hr class="my-4 border-gray-200">');
        text = text.replace(/^- (.+)$/gm, '<li class="ml-4 list-disc text-gray-700">$1</li>');
        text = text.replace(/(<li[^>]*>.*<\/li>\n?)+/g, '<ul class="my-2 space-y-0.5">$&</ul>');
        text = text.replace(/^\d+\. (.+)$/gm, '<li class="ml-4 list-decimal text-gray-700">$1</li>');
        text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
            const safeUrl = url.trim();
            if (/^(https?:\/\/|\/|#|mailto:)/i.test(safeUrl)) {
                return `<a href="${escapeHtml(safeUrl)}" class="text-primary-600 hover:text-primary-700 underline decoration-primary-300" target="_blank" rel="noopener">${escapeHtml(label)}</a>`;
            }
            return escapeHtml(label);
        });
        text = text.replace(/\n\n/g, '</p><p class="mt-2">');
        text = `<p>${text}</p>`.replace(/<p>\s*<\/p>/g, '');

        return typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(text) : text;
    }

    function displayToolName(toolName) {
        return String(toolName || 'tool')
            .replace(/^memory-server-/, '')
            .replace(/^mcp_memory-server_/, '')
            .replace(/_/g, ' ');
    }

    function appendToolCall(container, toolName, input) {
        const toolId = `tool-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
        const wrapper = document.createElement('div');
        wrapper.className = 'tool-chip-wrapper';
        wrapper.dataset.tool = toolName;
        wrapper.dataset.toolId = toolId;

        let inputPreview = '';
        try {
            const parsed = typeof input === 'string' ? JSON.parse(input) : input;
            inputPreview = Object.entries(parsed || {})
                .filter(([, value]) => value != null)
                .map(([key, value]) => {
                    const rendered = typeof value === 'string' ? value : JSON.stringify(value);
                    return `<span class="text-gray-500">${escapeHtml(key)}:</span> <span class="text-gray-700">${escapeHtml(rendered.slice(0, 60))}</span>`;
                })
                .join(', ');
        } catch (_error) {
            inputPreview = escapeHtml(String(input || '').slice(0, 80));
        }

        wrapper.innerHTML = `
            <button type="button" data-tool-toggle="${toolId}" class="tool-chip flex items-center gap-2 w-full text-left px-3 py-2 rounded-lg border border-gray-200 bg-gray-50 hover:bg-gray-100 transition-colors text-xs group">
                <div class="tool-status-icon w-4 h-4 shrink-0">
                    <svg class="w-4 h-4 text-pink-500 animate-pulse" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M11.42 15.17 17.25 21A2.652 2.652 0 0 0 21 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766m0 0a4.5 4.5 0 0 0 6.229-6.476l-3.276 3.277a3.004 3.004 0 0 1-2.25-2.25l3.276-3.276a4.5 4.5 0 0 0-6.336 4.486c.049.58.025 1.194-.14 1.743Z" /></svg>
                </div>
                <div class="flex-1 min-w-0">
                    <span class="font-medium text-gray-900">${escapeHtml(displayToolName(toolName))}</span>
                    <span class="tool-inline-detail ml-1.5 text-gray-400">${inputPreview ? `· ${inputPreview}` : ''}</span>
                </div>
                <svg class="tool-chevron w-3 h-3 text-gray-400 transition-transform group-hover:text-gray-600" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>
            </button>
            <div id="details-${toolId}" class="tool-details">
                <div class="mt-1 ml-6 rounded-lg border border-gray-100 bg-gray-50 p-3 text-xs font-mono">
                    <div class="mb-2">
                        <span class="font-sans text-[10px] uppercase tracking-wide text-gray-500">Input</span>
                        <pre class="mt-1 whitespace-pre-wrap break-words text-gray-700">${escapeHtml(formatJSON(input))}</pre>
                    </div>
                    <div class="tool-output-section hidden">
                        <span class="font-sans text-[10px] uppercase tracking-wide text-gray-500">Output</span>
                        <pre class="tool-output mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap break-words text-green-700"></pre>
                    </div>
                </div>
            </div>`;
        container.appendChild(wrapper);
        return wrapper;
    }

    function updateToolResult(container, toolName, output) {
        const chips = container.querySelectorAll(`.tool-chip-wrapper[data-tool="${CSS.escape(String(toolName))}"]`);
        for (let index = chips.length - 1; index >= 0; index--) {
            const chip = chips[index];
            const outputSection = chip.querySelector('.tool-output-section');
            if (!outputSection || !outputSection.classList.contains('hidden')) continue;

            chip.querySelector('.tool-status-icon').innerHTML = '<svg class="w-4 h-4 text-green-600" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5" /></svg>';
            const outputText = typeof output === 'string'
                ? output
                : (JSON.stringify(output ?? '') || String(output ?? ''));
            const preview = String(outputText || '').replace(/[\n\r]+/g, ' ').slice(0, 120);
            const inlineDetail = chip.querySelector('.tool-inline-detail');
            if (inlineDetail && preview) {
                inlineDetail.innerHTML = `· <span class="text-green-600">${escapeHtml(preview)}${outputText.length > 120 ? '…' : ''}</span>`;
            }
            outputSection.classList.remove('hidden');
            chip.querySelector('.tool-output').textContent = formatJSON(output);
            return chip;
        }
    }

    function finalizeToolCalls(container) {
        container.querySelectorAll('.tool-status-icon .animate-pulse').forEach(spinner => {
            spinner.closest('.tool-status-icon').innerHTML = '<svg class="w-4 h-4 text-green-600" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5" /></svg>';
        });
    }

    // ── Hook-call chip ──────────────────────────────────────────────────
    // Variant of the tool chip: same wrapper/details/toggle chrome and the
    // same details-id scheme, but a distinct identity — brand-pink accent
    // (--lucent-pink) plus a hook icon set. Collapsed by default; expanded
    // view shows hook name, trigger event, the matched tool call, and a
    // compact summary of injected memories.

    const HOOK_ACCENT = '#ff3f9b';

    const HOOK_ICONS = {
        memory: '<svg class="hook-icon" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 4.5A1.5 1.5 0 0 1 7.5 3h9A1.5 1.5 0 0 1 18 4.5V21l-6-3.75L6 21V4.5Z" /></svg>',
        static: '<svg class="hook-icon" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" /></svg>',
        command: '<svg class="hook-icon" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6.75 7.5l3 2.25-3 2.25m4.5 0h3m-9 8.25h13.5A2.25 2.25 0 0 0 21 18V6a2.25 2.25 0 0 0-2.25-2.25H5.25A2.25 2.25 0 0 0 3 6v12a2.25 2.25 0 0 0 2.25 2.25Z" /></svg>',
        default: '<svg class="hook-icon" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M17.25 6.75 22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3-4.5 16.5" /></svg>',
    };

    function hookIcon(hookName) {
        const name = String(hookName || '').toLowerCase();
        if (name.includes('memory')) return HOOK_ICONS.memory;
        if (name.includes('static')) return HOOK_ICONS.static;
        if (name.includes('command')) return HOOK_ICONS.command;
        return HOOK_ICONS.default;
    }

    function hookPhaseLabel(phase) {
        const labels = {
            before_tool_call: 'before tool call',
            after_tool_call: 'after tool call',
            before_model_call: 'before model call',
        };
        return labels[phase] || String(phase || '').replace(/_/g, ' ');
    }

    function normalizeHookEvent(payload) {
        const root = (payload && typeof payload === 'object') ? payload : {};
        // One normalizer for every shape the chip can receive: live SSE
        // payloads (structured fields on the payload itself, or nested under
        // `metadata` on older servers) and replay rows from the sessions API
        // (structured fields nested under `raw.raw` after persistence).
        const sources = [root];
        for (const key of ['metadata', 'raw']) {
            const nested = root[key];
            if (nested && typeof nested === 'object') sources.push(nested);
        }
        for (const nested of sources.slice()) {
            if (nested && typeof nested.raw === 'object' && nested.raw) sources.push(nested.raw);
        }
        const pick = (key) => {
            for (const source of sources) {
                if (source && source[key] != null) return source[key];
            }
            return null;
        };
        const asString = (value) => (typeof value === 'string' && value.trim() ? value.trim() : null);
        const asList = (value) => (Array.isArray(value) ? value : []);

        let memories = asList(pick('injected') || pick('memories'))
            .filter(memory => memory && typeof memory === 'object');
        const fileRefs = asList(pick('file_refs')).filter(ref => typeof ref === 'string');
        const rawMemoryCount = pick('memory_count');
        let memoryCount = Number.isInteger(rawMemoryCount) ? rawMemoryCount : null;
        const text = asString(pick('text')) || asString(pick('content')) || asString(pick('detail')) || '';

        // Legacy persisted rows (servers before the hook-SSE enrichment) carry
        // only the injection text — recover a structured summary from it.
        if (!memories.length && text) {
            const parsedMemories = [];
            const parsedRefs = [];
            for (const line of text.split('\n')) {
                const memoryMatch = line.match(/^- ([0-9a-f]{8})(?:\s+\[([^\]]+)\])?:\s?(.*)$/);
                if (memoryMatch) {
                    parsedMemories.push({
                        id: memoryMatch[1],
                        tags: memoryMatch[2]
                            ? memoryMatch[2].split(',').map(tag => tag.trim()).filter(Boolean)
                            : [],
                        content: memoryMatch[3],
                    });
                    continue;
                }
                const refMatch = line.match(/^-\s+`([^`]+)`\s*$/);
                if (refMatch) parsedRefs.push(refMatch[1]);
            }
            if (parsedMemories.length) memories = parsedMemories;
            if (!fileRefs.length && parsedRefs.length) fileRefs.push(...parsedRefs.slice(0, 3));
        }
        if (memoryCount == null) memoryCount = memories.length;

        // Project-context events (system-prompt injection, Projects v2):
        // name/file_count/bytes ride the emitting engine's event metadata
        // and describe the injected standing block — not tool activity.
        // Legacy rows without metadata recover the name from the block text.
        let projectName = asString(pick('name'));
        let fileCount = pick('file_count');
        let blockBytes = pick('bytes');
        if (!projectName) {
            const projMatch = text.match(/^## Project:\s*(.+)$/m);
            projectName = projMatch ? projMatch[1].trim() : null;
        }
        if (!Number.isInteger(fileCount)) fileCount = null;
        if (!Number.isInteger(blockBytes)) blockBytes = null;

        // Message-injection decision gate (memory.inject_decision_gate_*):
        // model id, how many candidates it gated out, and per-memory
        // relevance scores keyed by memory-id prefix (matching the
        // "memories" entries). Absent when the gate is off or on legacy rows.
        const decisionModel = asString(pick('decision_model'));
        const decisionGatedRaw = pick('decision_gated');
        const decisionGated = Number.isInteger(decisionGatedRaw) ? decisionGatedRaw : null;
        let decisionScores = null;
        const decisionScoresRaw = pick('decision_scores');
        if (decisionScoresRaw && typeof decisionScoresRaw === 'object' && !Array.isArray(decisionScoresRaw)) {
            decisionScores = {};
            for (const [key, value] of Object.entries(decisionScoresRaw)) {
                const num = Number(value);
                if (Number.isFinite(num)) decisionScores[key] = num;
            }
        }
        const decisionSkipped = asString(pick('decision_skipped'));

        return {
            hookName: asString(pick('hook')) || 'hook',
            phase: asString(pick('phase')),
            triggerTool: asString(pick('trigger_tool')),
            decision: asString(pick('decision')) || 'inject',
            fileRefs,
            memories,
            memoryCount,
            projectName,
            fileCount,
            blockBytes,
            decisionModel,
            decisionGated,
            decisionScores,
            decisionSkipped,
            text,
        };
    }

    function appendHookCall(container, payload) {
        const hook = normalizeHookEvent(payload);
        const hookId = `hook-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
        const wrapper = document.createElement('div');
        wrapper.className = 'tool-chip-wrapper hook-chip-wrapper';
        wrapper.dataset.hook = hook.hookName;
        wrapper.dataset.hookId = hookId;

        const inlineParts = [];
        if (hook.phase) inlineParts.push(hookPhaseLabel(hook.phase));
        if (hook.memoryCount) inlineParts.push(`${hook.memoryCount} ${hook.memoryCount === 1 ? 'memory' : 'memories'}`);
        if (hook.projectName) {
            const summaryParts = [];
            if (Number.isInteger(hook.fileCount)) summaryParts.push(`${hook.fileCount} ${hook.fileCount === 1 ? 'file' : 'files'}`);
            if (Number.isInteger(hook.blockBytes)) summaryParts.push(`${hook.blockBytes} B block`);
            inlineParts.push(summaryParts.length
                ? `${hook.projectName} · ${summaryParts.join(' · ')}`
                : hook.projectName);
        }
        const inlineDetail = inlineParts.length
            ? `· <span class="hook-inline-accent">${inlineParts.map(part => escapeHtml(part)).join(' · ')}</span>`
            : '';

        const detailLabel = (text) => `<span class="hook-detail-label">${escapeHtml(text)}</span>`;
        const triggerBlock = hook.triggerTool
            ? `<div class="hook-detail-row">${detailLabel('Tool call')}<span class="hook-detail-value font-medium">${escapeHtml(displayToolName(hook.triggerTool))}</span></div>`
            : '';
        const phaseBlock = hook.phase
            ? `<div class="hook-detail-row">${detailLabel('Trigger event')}<span class="hook-detail-value">${escapeHtml(hookPhaseLabel(hook.phase))}</span></div>`
            : '';
        const projectBlock = hook.projectName
            ? `<div class="hook-detail-row">${detailLabel('Injected project block')}<span class="hook-detail-value font-medium">${escapeHtml(hook.projectName)}${Number.isInteger(hook.fileCount) ? ` · ${hook.fileCount} ${hook.fileCount === 1 ? 'file' : 'files'}` : ''}${Number.isInteger(hook.blockBytes) ? ` · ${hook.blockBytes} B` : ''}</span></div>`
            : '';
        const decisionBlock = hook.decision && hook.decision !== 'inject'
            ? `<div class="hook-detail-row">${detailLabel('Decision')}<span class="hook-detail-value hook-decision hook-decision-${escapeHtml(hook.decision)}">${escapeHtml(hook.decision)}</span></div>`
            : '';
        const refsBlock = hook.fileRefs.length
            ? `<div class="hook-detail-row hook-detail-column">${detailLabel('Matched files')}<div class="hook-ref-list">${hook.fileRefs.map(ref => `<span class="hook-file-ref">${escapeHtml(ref)}</span>`).join('')}</div></div>`
            : '';
        // Relevance-gate summary: only when the decision-model screen ran
        // (its model id is present). Reports the model, how many candidates
        // it gated out, or the skip reason when the screen failed open.
        const gateParts = [];
        if (hook.decisionModel) gateParts.push(escapeHtml(hook.decisionModel));
        if (Number.isInteger(hook.decisionGated) && hook.decisionGated > 0) gateParts.push(`${hook.decisionGated} gated out`);
        if (hook.decisionGated === 0 && hook.decisionModel && !hook.decisionSkipped) gateParts.push('kept all');
        let gateBlock = '';
        if (hook.decisionModel || hook.decisionSkipped) {
            const gateValue = hook.decisionSkipped
                ? `skipped${hook.decisionSkipped.length ? ` — ${hook.decisionSkipped}` : ''}${gateParts.length ? ` · ${gateParts.join(' · ')}` : ''}`
                : gateParts.join(' · ');
            gateBlock = `<div class="hook-detail-row">${detailLabel('Relevance gate')}<span class="hook-detail-value">${gateValue ? escapeHtml(gateValue) : 'ran'}</span></div>`;
        }
        const memoryItems = hook.memories.map(memory => {
            const tags = (memory.tags || []).map(tag => `<span class="hook-memory-tag">${escapeHtml(tag)}</span>`).join('');
            const score = hook.decisionScores && Number.isFinite(hook.decisionScores[memory.id])
                ? ` <span class="hook-memory-score" title="Decision-model relevance">${hook.decisionScores[memory.id].toFixed(2)}</span>`
                : '';
            return `<li class="hook-memory-item"><span class="hook-memory-id">${escapeHtml(memory.id || '')}</span>${score}${tags ? `<span class="hook-memory-tags">${tags}</span>` : ''}<span class="hook-memory-content">${escapeHtml(memory.content || '')}</span></li>`;
        }).join('');
        const memoriesBlock = memoryItems
            ? `<div class="hook-detail-row hook-detail-column">${detailLabel(`Injected memories (${hook.memories.length})`)}<ul class="hook-memory-list">${memoryItems}</ul></div>`
            : '';
        const contextLabel = hook.decision === 'block' ? 'Block message' : 'Injected context';
        const contextBlock = (!memoryItems && hook.text)
            ? `<div class="hook-detail-row hook-detail-column">${detailLabel(contextLabel)}<pre class="hook-context-text">${escapeHtml(hook.text)}</pre></div>`
            : '';

        wrapper.innerHTML = `
            <button type="button" data-tool-toggle="${hookId}" class="tool-chip hook-chip flex items-center gap-2 w-full text-left px-3 py-2 rounded-lg border border-gray-200 bg-gray-50 hover:bg-gray-100 transition-colors text-xs group">
                <div class="hook-status-icon w-4 h-4 shrink-0">${hookIcon(hook.hookName)}</div>
                <div class="flex-1 min-w-0">
                    <span class="font-medium text-gray-900">${escapeHtml(displayToolName(hook.hookName))}</span>
                    <span class="tool-inline-detail ml-1.5">${inlineDetail}</span>
                </div>
                <svg class="tool-chevron w-3 h-3 text-gray-400 transition-transform group-hover:text-gray-600" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>
            </button>
            <div id="details-${hookId}" class="tool-details">
                <div class="mt-1 ml-6 hook-details rounded-lg border p-3 text-xs font-mono">
                    ${triggerBlock}${phaseBlock}${projectBlock}${decisionBlock}${gateBlock}${refsBlock}${memoriesBlock}${contextBlock}
                </div>
            </div>`;
        container.appendChild(wrapper);
        return wrapper;
    }

    // ---------------------------------------------------------------------
    // Delegated-work report cards
    // ---------------------------------------------------------------------
    // A background task/request result is rendered as a self-contained card in
    // the conversation, not as a tool chip: it is a report from a sub-agent, and
    // it reads as one. The card is driven entirely by the `subagent_report`
    // event payload, so streaming and history replay render identically.
    const REPORT_ACCENTS = {
        success: { bar: 'bg-emerald-500', chip: 'bg-emerald-50 text-emerald-700 border-emerald-200', label: 'Completed' },
        attention: { bar: 'bg-red-500', chip: 'bg-red-50 text-red-700 border-red-200', label: 'Needs attention' },
        // `review` is Lucent's own automatic post-completion check, which
        // resolves on its own within about a minute. It is NOT a human approval
        // gate, so the label must not read as "waiting on you" — that wording
        // left finished work looking like it needed a decision.
        review: { bar: 'bg-sky-500', chip: 'bg-sky-100 text-sky-700 border-sky-200', label: 'Finishing up' },
        muted: { bar: 'bg-gray-400', chip: 'bg-gray-100 text-gray-600 border-gray-200', label: 'Stopped' },
        info: { bar: 'bg-primary-500', chip: 'bg-primary-50 text-primary-700 border-primary-200', label: 'Update' },
    };

    function reportAccent(severity) {
        return REPORT_ACCENTS[severity] || REPORT_ACCENTS.info;
    }

    /**
     * Which progress cards the person has explicitly collapsed.
     *
     * Kept on the transcript element so it survives re-renders without a
     * module-level global: the card is rebuilt on every status change, and a
     * collapse the user chose must not spring back open.
     */
    function progressCollapsedIds() {
        const host = document.getElementById('messages');
        if (!host) return new Set();
        if (!host.__delegatedCollapsed) host.__delegatedCollapsed = new Set();
        return host.__delegatedCollapsed;
    }

    /**
     * Render (or refresh) the live progress card for one running request.
     *
     * This is a *projection of current state*, not a report. It is deliberately
     * separate from `appendSubagentReport`: that one renders a settled outcome,
     * this one shows where the work is right now and is updated in place as the
     * task tree advances. It disappears from the transcript once the request
     * settles, because the report card takes over at that point.
     *
     * Markup mirrors `.subagent-report-card` (accent bar, chip, paper surface)
     * so a progress box and a result box read as the same family of object
     * rather than two different widgets.
     *
     * @param {HTMLElement} container - transcript container (messages list)
     * @param {object} request - delegated-work request entry from the activity API
     */
    function upsertDelegatedProgress(container, request) {
        if (!container || !request || !request.id) return null;
        const id = String(request.id);
        const selector = `.delegated-progress[data-request-id="${CSS.escape(id)}"]`;
        let wrapper = container.querySelector(selector);
        const counts = request.task_counts || {};
        const total = Number(request.task_total || 0);
        const done = Number(counts.completed || 0);
        const running = Number(counts.running || 0) + Number(counts.claimed || 0);
        const failed = Number(counts.failed || 0);
        const pct = total ? Math.round(((done + failed) / total) * 100) : 0;

        if (!wrapper) {
            wrapper = document.createElement('div');
            wrapper.className = 'delegated-progress msg-enter px-6 py-2';
            wrapper.dataset.requestId = id;
            container.appendChild(wrapper);
        }
        const collapsed = progressCollapsedIds();
        // Only rewrite when the visible facts changed — a no-op render would
        // restart the progress-bar animation and read as a flicker.
        const fingerprint = [
            String(request.status || ''),
            String(total),
            String(done),
            String(running),
            String(failed),
            String(request.title || ''),
            collapsed.has(id) ? 'closed' : 'open',
            (request.tasks || []).map((t) => `${t.id}:${t.status}`).join(','),
        ].join('|');
        if (wrapper.dataset.fingerprint === fingerprint) return wrapper;
        wrapper.dataset.fingerprint = fingerprint;

        const isOpen = !collapsed.has(id);
        const state = running ? 'running' : failed ? 'attention' : 'success';
        const statusText = request.status_label
            || (running ? 'running'
                : request.status === 'review' ? 'finishing up'
                    : String(request.status || ''));
        const taskRows = (request.tasks || [])
            .map((task) => {
                const st = String(task.status || '');
                const dot = st === 'completed' ? 'is-done'
                    : st === 'failed' ? 'is-failed'
                        : st === 'cancelled' ? 'is-cancelled'
                            : st === 'claimed' || st === 'running' ? 'is-running'
                                : 'is-queued';
                const label = st === 'completed' ? 'done'
                    : st === 'failed' ? 'failed'
                        : st === 'cancelled' ? 'cancelled'
                            : st === 'needs_review' ? 'needs a check'
                                : st === 'claimed' ? 'starting'
                                    : st === 'running' ? 'running'
                                        : 'queued';
                return `<li class="delegated-progress-task">
                    <span class="delegated-progress-dot ${dot}"></span>
                    <span class="delegated-progress-task-title">${escapeHtml(task.title || 'Task')}</span>
                    <span class="delegated-progress-task-status">${escapeHtml(label)}</span>
                </li>`;
            })
            .join('');

        wrapper.dataset.state = state;
        wrapper.innerHTML = `
            <details class="delegated-progress-card"${isOpen ? ' open' : ''}>
                <summary class="delegated-progress-summary">
                    <svg class="delegated-progress-chevron" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" aria-hidden="true">
                        <path stroke-linecap="round" stroke-linejoin="round" d="m9 18 6-6-6-6" />
                    </svg>
                    <span class="delegated-progress-chip">
                        ${running ? '<span class="delegated-progress-spinner" aria-hidden="true"></span>' : ''}
                        ${escapeHtml(statusText)}
                    </span>
                    <span class="delegated-progress-title" title="${escapeHtml(request.title || '')}">${escapeHtml(request.title || 'Delegated work')}</span>
                    <span class="delegated-progress-count">${done}/${total || 0}</span>
                </summary>
                <div class="delegated-progress-detail">
                    <div class="delegated-progress-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">
                        <span class="delegated-progress-fill" style="width:${pct}%"></span>
                    </div>
                    ${taskRows ? `<ul class="delegated-progress-tasks">${taskRows}</ul>` : ''}
                    <div class="delegated-progress-footer">
                        <span class="delegated-progress-meta">${done}/${total || 0} tasks done${running ? ` · ${running} running` : ''}${failed ? ` · ${failed} failed` : ''}</span>
                        <a href="/activity/${escapeHtml(id)}" class="delegated-progress-link">Open request</a>
                    </div>
                </div>
            </details>`;

        const details = wrapper.querySelector('details');
        if (details) {
            // No `once` here: the card is rebuilt whenever the status
            // fingerprint changes, so each rebuild needs its own listener. The
            // element is replaced on rebuild, so listeners cannot accumulate.
            details.addEventListener('toggle', () => {
                const set = progressCollapsedIds();
                if (details.open) set.delete(id);
                else set.add(id);
            });
        }
        return wrapper;
    }

    /**
     * Drop progress cards whose request is no longer live.
     *
     * The report card is the settled record, so leaving a progress card behind
     * would show two competing views of the same request.
     */
    function pruneDelegatedProgress(container, liveRequestIds) {
        if (!container) return;
        const live = new Set((liveRequestIds || []).map(String));
        container.querySelectorAll('.delegated-progress').forEach((node) => {
            if (!live.has(String(node.dataset.requestId || ''))) node.remove();
        });
    }

    /**
     * Render a delegated-work report card.
     *
     * @param {HTMLElement} container - per-assistant tools container
     * @param {object} payload - subagent_report event (`raw` body or flattened)
     */
    function appendSubagentReport(container, payload) {
        const data = payload && payload.raw ? payload.raw : (payload || {});
        const kind = String(data.report_kind || data.kind || '');
        const status = kind.includes('.') ? kind.split('.').slice(1).join('.') : '';
        const severity = data.severity || (
            status === 'completed' ? 'success'
                : status === 'failed' || status === 'needs_rework' ? 'attention'
                    : status === 'review' || status === 'needs_review' ? 'review'
                        : status === 'cancelled' ? 'muted'
                            : 'info'
        );
        const accent = reportAccent(severity);

        const request = data.request || {};
        const task = data.task || null;
        const headline = data.headline || payload.detail || 'Delegated work update';
        const body = data.body || '';
        const url = data.url || (request.id ? `/activity/${request.id}` : '');

        const wrapper = document.createElement('div');
        wrapper.className = 'subagent-report';
        wrapper.dataset.reportKind = kind;
        wrapper.dataset.severity = severity;

        const metaBits = [];
        if (task && task.status_label) metaBits.push(`Task: ${task.status_label}`);
        if (request.status_label) metaBits.push(`Request: ${request.status_label}`);

        wrapper.innerHTML = `
            <div class="subagent-report-card">
                <span class="subagent-report-accent ${accent.bar}"></span>
                <div class="subagent-report-body">
                    <div class="subagent-report-header">
                        <span class="subagent-report-chip ${accent.chip}">
                            <svg class="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke-width="1.8" stroke="currentColor" aria-hidden="true">
                                <path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09Z" />
                            </svg>
                            ${escapeHtml(accent.label)}
                        </span>
                        ${request.title ? `<span class="subagent-report-request" title="${escapeHtml(request.title)}">${escapeHtml(request.title)}</span>` : ''}
                    </div>
                    <p class="subagent-report-headline">${escapeHtml(headline)}</p>
                    ${body ? `<p class="subagent-report-text">${escapeHtml(body)}</p>` : ''}
                    <div class="subagent-report-footer">
                        <span class="subagent-report-meta">${escapeHtml(metaBits.join(' · '))}</span>
                        ${url ? `<a href="${escapeHtml(url)}" class="subagent-report-link">Open request</a>` : ''}
                    </div>
                </div>
            </div>`;
        container.appendChild(wrapper);
        return wrapper;
    }

    // ---------------------------------------------------------------------
    // Human-in-the-loop interaction cards (tool approval + clarifying question)
    // ---------------------------------------------------------------------
    // Two families, one visual language: a bordered card that sits inline in the
    // assistant row where the tool call or question happened, with the decision
    // rendered in the card once it is made. Replaying a session rebuilds the
    // same card in its resolved state, so history never claims a tool ran
    // silently when the person was actually asked.
    //
    // The card never performs requests itself: it puts the answer in
    // `data-lucent-interaction` and lets the host page POST, because only the
    // host knows its CSRF and routing conventions.

    const INTERACTION_LABELS = {
        approve: { text: 'Approved', tone: 'approve' },
        deny: { text: 'Denied', tone: 'deny' },
        expired: { text: 'No answer', tone: 'muted' },
    };

    function interactionRiskNote(risk) {
        if (risk === 'execute') return 'Runs code or a sandboxed command';
        if (risk === 'write') return 'Changes data';
        return 'Reads only';
    }

    function interactionPayload(payload) {
        const root = payload && typeof payload === 'object' ? payload : {};
        // Same shapes the hook chip has to survive: a live SSE payload puts the
        // fields at the top level, while a replayed row from the sessions API
        // nests them under `raw.raw` (the persistence layer stores the payload
        // there so replay can rebuild the exact card). Walk down until the
        // request id appears, bounded so a malformed row cannot loop.
        let source = root;
        for (let depth = 0; depth < 3; depth += 1) {
            if (source.request_id) break;
            const nested = source.raw;
            if (!nested || typeof nested !== 'object') break;
            source = nested;
        }
        const resolved = source.request_id || root.request_id ? source : { ...root, ...source };
        // Questions arrive as a batch. Older payloads (and approvals) carry the
        // single-question fields, so synthesize a one-entry list for them — the
        // renderer then only ever deals with a list.
        let questions = Array.isArray(resolved.questions) ? resolved.questions : [];
        if (!questions.length && resolved.kind !== 'approval' && resolved.question) {
            questions = [{
                question: resolved.question,
                header: resolved.header || '',
                choices: Array.isArray(resolved.choices)
                    ? resolved.choices.map(choice => (
                        typeof choice === 'string' ? { label: choice } : choice
                    ))
                    : [],
                allow_free_form: resolved.allow_freeform !== false,
            }];
        }
        return {
            requestId: resolved.request_id || root.event_id || '',
            kind: resolved.kind === 'question' ? 'question' : 'approval',
            sessionId: resolved.session_id || '',
            tool: resolved.tool || '',
            input: resolved.input,
            question: resolved.question || '',
            header: resolved.header || '',
            choices: Array.isArray(resolved.choices) ? resolved.choices.filter(Boolean) : [],
            allowFreeform: resolved.allow_freeform !== false,
            risk: resolved.risk || 'write',
            expiresAt: resolved.expires_at,
            questions: questions.map(question => ({
                question: question.question || '',
                header: question.header || '',
                // Free text is always offered: options are a shortcut, not a cage.
                allowFreeForm: question.allow_free_form !== false,
                choices: (Array.isArray(question.choices) ? question.choices : [])
                    .map(choice => (
                        typeof choice === 'string'
                            ? { label: choice, description: '', recommended: false }
                            : {
                                label: choice.label || choice.value || '',
                                description: choice.description || '',
                                recommended: choice.recommended === true,
                            }
                    ))
                    .filter(choice => choice.label),
            })),
        };
    }

    function interactionPreview(input) {
        if (input == null || input === '') return '';
        try {
            const parsed = typeof input === 'string' ? JSON.parse(input) : input;
            if (parsed && typeof parsed === 'object') {
                return Object.entries(parsed)
                    .filter(([, value]) => value != null && value !== '')
                    .slice(0, 4)
                    .map(([key, value]) => {
                        const rendered = typeof value === 'string' ? value : JSON.stringify(value);
                        return `${key}: ${String(rendered).slice(0, 80)}`;
                    })
                    .join('\n');
            }
        } catch (_error) {
            /* fall through to raw text */
        }
        return String(input).slice(0, 240);
    }

    /**
     * Append a tool-approval or clarifying-question card.
     *
     * @param {HTMLElement} container - the assistant row's tools container
     * @param {object} payload - the live SSE or replayed event
     * @returns {HTMLElement} the card wrapper
     */
    function appendInteractionRequest(container, payload) {
        const data = interactionPayload(payload);
        const wrapper = document.createElement('div');
        wrapper.className = 'interaction-card';
        wrapper.dataset.interactionKind = data.kind;
        wrapper.dataset.interactionId = data.requestId;
        wrapper.dataset.sessionId = data.sessionId;

        const isApproval = data.kind === 'approval';
        const preview = isApproval ? interactionPreview(data.input) : '';
        const questionCount = data.questions.length;
        const title = isApproval
            ? `Run ${displayToolName(data.tool)}?`
            : (questionCount > 1
                ? `${questionCount} questions`
                : (data.questions[0]?.header || data.header || 'Question'));
        const prompt = isApproval
            ? interactionRiskNote(data.risk)
            : (questionCount > 1 ? '' : (data.questions[0]?.question || data.question));

        // Each question gets its own block: a choice row, the recommended hint,
        // and a free-text field that is always present. Free text is the escape
        // hatch when the model's options do not cover the real answer, so it is
        // never hidden behind a disclosure.
        const questionBlocks = isApproval ? '' : data.questions.map((question, index) => {
            const choices = question.choices.length
                ? `<div class="interaction-choices" role="group">${question.choices
                    .map((choice, choiceIndex) => `
                        <button type="button"
                                class="interaction-choice${choice.recommended ? ' is-recommended' : ''}"
                                data-interaction-choice="${escapeHtml(choice.label)}"
                                data-interaction-question="${index}"
                                data-interaction-choice-index="${choiceIndex}"
                                title="${escapeHtml(choice.description || choice.label)}">
                            <span class="interaction-choice-label">${escapeHtml(choice.label)}</span>
                            ${choice.recommended
                                ? '<span class="interaction-choice-badge">Recommended</span>'
                                : ''}
                            ${choice.description
                                ? `<span class="interaction-choice-description">${escapeHtml(choice.description)}</span>`
                                : ''}
                        </button>`)
                    .join('')}</div>`
                : '';
            return `
                <div class="interaction-question" data-interaction-question-block="${index}">
                    <div class="interaction-question-head">
                        ${question.header ? `<span class="interaction-question-header">${escapeHtml(question.header)}</span>` : ''}
                        <span class="interaction-question-text">${escapeHtml(question.question)}</span>
                    </div>
                    ${choices}
                    ${question.allowFreeForm
                        ? `<input type="text" class="interaction-note interaction-answer-field"
                                  data-interaction-answer="${index}" maxlength="8000"
                                  placeholder="${question.choices.length ? 'Or type your own answer' : 'Type your answer'}" />`
                        : ''}
                </div>`;
        }).join('');

        const freeform = isApproval
            ? `<input type="text" class="interaction-note" data-interaction-note maxlength="2000"
                      placeholder="Optional: tell the model why (or what to do instead)" />`
            : '';

        const actions = isApproval
            ? `<div class="interaction-actions">
                   <button type="button" class="interaction-btn interaction-btn-approve" data-interaction-decision="approve">Allow</button>
                   <button type="button" class="interaction-btn interaction-btn-deny" data-interaction-decision="deny">Deny</button>
                   <label class="interaction-remember">
                       <input type="checkbox" data-interaction-remember />
                       <span>Always allow in this chat</span>
                   </label>
               </div>`
            : `<div class="interaction-actions">
                   <button type="button" class="interaction-btn interaction-btn-approve" data-interaction-decision="answer">${questionCount > 1 ? 'Send answers' : 'Send answer'}</button>
                   <button type="button" class="interaction-btn interaction-btn-quiet" data-interaction-decision="skip">Skip</button>
               </div>`;

        wrapper.innerHTML = `
            <div class="interaction-card-inner">
                <div class="interaction-card-head">
                    <span class="interaction-card-icon" aria-hidden="true">
                        ${isApproval
                            ? '<svg fill="none" viewBox="0 0 24 24" stroke-width="1.7" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751A11.959 11.959 0 0 1 12 2.714Z" /></svg>'
                            : '<svg fill="none" viewBox="0 0 24 24" stroke-width="1.7" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.172 1.025 1.172 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.45.999-1.45 1.827v.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 5.25h.008v.008H12v-.008Z" /></svg>'}
                    </span>
                    <span class="interaction-card-title">${escapeHtml(title)}</span>
                    <span class="interaction-card-state" data-interaction-state></span>
                </div>
                ${prompt ? `<p class="interaction-card-prompt">${escapeHtml(prompt)}</p>` : ''}
                ${preview ? `<pre class="interaction-card-input">${escapeHtml(preview)}</pre>` : ''}
                ${questionBlocks}
                ${freeform}
                ${actions}
            </div>`;
        container.appendChild(wrapper);
        return wrapper;
    }

    /**
     * Normalize an outcome payload (live SSE or replayed row) to a flat object.
     *
     * Replay rows nest the payload one level deeper than live events, so the
     * decision fields have to be unwrapped by the same rule the request cards
     * use or a replayed answer is invisible.
     *
     * @param {object} payload
     * @returns {object} flat outcome with request_id/decision/answer/reason
     */
    function interactionOutcome(payload) {
        let source = payload && typeof payload === 'object' ? payload : {};
        for (let depth = 0; depth < 3; depth += 1) {
            if (source.request_id) break;
            const nested = source.raw;
            if (!nested || typeof nested !== 'object') break;
            source = nested;
        }
        return source;
    }

    /**
     * Render the outcome on an interaction card (live answer or history replay).
     *
     * @param {HTMLElement} wrapper - card returned by appendInteractionRequest
     * @param {object} outcome - `{decision, answer, choice, reason, answered}`
     */
    function resolveInteractionCard(wrapper, outcome) {
        if (!wrapper) return;
        const data = outcome || {};
        const isApproval = wrapper.dataset.interactionKind === 'approval';
        const stateEl = wrapper.querySelector('[data-interaction-state]');
        const decision = String(data.decision || '');
        // Batched answers arrive as `answers`; a single question sends one string.
        // Normalize here so the resolved renderer has one shape to read.
        const answers = Array.isArray(data.answers)
            ? data.answers
            : (data.answer || data.choice
                ? [{ answer: data.answer, choice: data.choice }]
                : []);
        const answered = data.answered === true
            || answers.some(entry => entry && (entry.answer || entry.choice));

        let label = '';
        if (isApproval) {
            const meta = INTERACTION_LABELS[decision] || INTERACTION_LABELS.expired;
            label = decision === 'approve' ? 'Allowed' : (decision === 'deny' ? 'Denied' : 'No answer');
            wrapper.dataset.interactionState = meta.tone;
        } else if (answered) {
            label = 'Answered';
            wrapper.dataset.interactionState = 'approve';
        } else {
            label = 'Skipped';
            wrapper.dataset.interactionState = 'muted';
        }

        if (stateEl) stateEl.textContent = label;
        wrapper.classList.add('is-resolved');
        // Drop the controls: the decision is made and cannot be replayed, so
        // leaving live buttons would invite a click that always 409s.
        wrapper.querySelectorAll(
            '.interaction-actions, .interaction-choices, .interaction-freeform, ' +
            '[data-interaction-note], [data-interaction-answer]'
        ).forEach(node => node.remove());

        const inner = wrapper.querySelector('.interaction-card-inner');
        const reason = String(data.reason || data.note || '').trim();
        if (reason) {
            const note = document.createElement('p');
            note.className = 'interaction-card-answer';
            note.textContent = reason;
            inner.appendChild(note);
        }

        if (isApproval) return;

        // Rebuild each answered question in place so the transcript reads as a
        // Q&A rather than a bare "Answered" chip. Per-question rendering also
        // survives a batch where only some questions were answered.
        const blocks = [...wrapper.querySelectorAll('[data-interaction-question-block]')];
        blocks.forEach((block, index) => {
            const entry = answers[index] || {};
            const text = String(entry.answer || entry.choice || '').trim();
            const target = block.querySelector('.interaction-question-head');
            if (!target) return;
            const value = document.createElement('span');
            value.className = 'interaction-question-answer';
            value.textContent = text || 'No answer';
            if (!text) value.classList.add('is-missing');
            target.appendChild(value);
        });
        // A single-question card without a per-question block (older payloads).
        if (!blocks.length && answers.length) {
            const text = String(answers[0].answer || answers[0].choice || '').trim();
            if (text) {
                const note = document.createElement('p');
                note.className = 'interaction-card-answer';
                note.textContent = text;
                inner.appendChild(note);
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    // Transcript rendering
    // ═══════════════════════════════════════════════════════════════════
    // One renderer for every conversation surface. /chat and a Handoff are the
    // same object — a persisted LLM session with a transcript — so they must
    // not drift into two implementations that each slowly grow their own idea
    // of what a tool chip, a question card, or a replayed turn looks like.

    function attachmentDataUrl(att) {
        if (att.dataUrl) return att.dataUrl;
        if (att.data && att.mime_type) return `data:${att.mime_type};base64,${att.data}`;
        return '';
    }

    function renderMessageAttachmentsHtml(attachments) {
        if (!attachments || !attachments.length) return '';
        const items = attachments.map(att => {
            const kind = att.kind || (att.mime_type && att.mime_type.startsWith('image/') ? 'image' : 'document');
            if (kind === 'image') {
                const url = attachmentDataUrl(att);
                return `<a href="${url}" target="_blank" rel="noopener" class="block">
                    <img src="${url}" alt="${escapeHtml(att.name || 'image')}" class="max-h-44 max-w-[220px] rounded-lg border border-white/20 object-cover" />
                </a>`;
            }
            return `<div class="flex items-center gap-2 rounded-lg bg-white/15 px-2.5 py-1.5 text-xs">
                <svg class="w-4 h-4 shrink-0 opacity-90" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" /></svg>
                <span class="truncate max-w-[180px]">${escapeHtml(att.name || 'document')}</span>
            </div>`;
        }).join('');
        return `<div class="flex flex-wrap gap-2 justify-end mb-2">${items}</div>`;
    }

    function appendUserMessage(container, text, attachments) {
        const div = document.createElement('div');
        div.className = 'msg-enter px-6 py-4';
        const attachmentsHtml = renderMessageAttachmentsHtml(attachments);
        const textHtml = text
            ? `<div class="chat-user-bubble max-w-[75%] bg-primary-600 text-white rounded-2xl rounded-br-sm px-4 py-3 text-sm leading-relaxed shadow-sm">${escapeHtml(text)}</div>`
            : '';
        div.innerHTML = `
            <div class="max-w-3xl mx-auto flex flex-col items-end gap-1">
                ${attachmentsHtml}
                ${textHtml}
            </div>`;
        container.appendChild(div);
        return div;
    }

    function createAssistantMessage(container, options) {
        const opts = options || {};
        const div = document.createElement('div');
        div.className = 'msg-enter chat-assistant-bg px-6 py-5';
        if (opts.id) div.id = opts.id;
        const placeholder = opts.loading === false
            ? ''
            : '<div class="flex gap-1.5">' +
                '<span class="typing-dot w-2 h-2 bg-gray-300 rounded-full inline-block"></span>' +
                '<span class="typing-dot w-2 h-2 bg-gray-300 rounded-full inline-block"></span>' +
                '<span class="typing-dot w-2 h-2 bg-gray-300 rounded-full inline-block"></span>' +
              '</div>';
        div.innerHTML = `
            <div class="assistant-response max-w-3xl mx-auto">
                <div class="min-w-0">
                    <div class="tools-container mb-3 space-y-2"></div>
                    <div class="assistant-content text-sm text-gray-800 leading-relaxed prose prose-sm prose-gray max-w-none">
                        ${placeholder}
                    </div>
                </div>
            </div>`;
        container.appendChild(div);
        return div;
    }

    function appendAssistantMessage(container, text, options) {
        const assistantEl = createAssistantMessage(container, options);
        fillAssistantMessage(assistantEl, text);
        return assistantEl;
    }

    function fillAssistantMessage(assistantEl, text) {
        const contentEl = assistantEl.querySelector('.assistant-content');
        contentEl.classList.remove('streaming-cursor');
        contentEl.innerHTML = renderMarkdown(text || '');
    }

    function appendAutoSelectionNote(assistantEl, info) {
        // Quiet provenance line for "Auto" turns: what ran, and how confident
        // the arbiter was (or why it fell back to the default chain).
        if (!assistantEl || !info || !info.chosen) return;
        const note = document.createElement('div');
        note.className = 'auto-selection-note mt-2 text-xs text-gray-400';
        let text = info.source === 'arbiter'
            ? `Auto picked ${info.chosen}`
            : `Auto used the default chain → ${info.chosen}`;
        if (info.source === 'arbiter' && typeof info.confidence === 'number') {
            text += ` (confidence ${info.confidence.toFixed(2)})`;
        }
        if (info.fallback_reason) text += ` — ${info.fallback_reason}`;
        note.textContent = text;
        const container = assistantEl.querySelector('.assistant-response > .min-w-0');
        (container || assistantEl).appendChild(note);
    }

    function createReasoningBlock(container) {
        const div = document.createElement('div');
        div.className = 'reasoning-wrapper mb-2';
        div.innerHTML = `
            <button data-reasoning-toggle="true" class="flex items-center gap-2 w-full text-left px-3 py-2 rounded-lg border border-pink-200 bg-pink-50 hover:bg-pink-100 transition-colors text-xs group">
                <svg class="w-3.5 h-3.5 text-pink-600 animate-pulse reasoning-icon" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
                </svg>
                <span class="font-medium text-pink-800">Thinking</span>
                <span class="reasoning-preview flex-1 text-pink-600 truncate ml-1"></span>
                <svg class="w-3 h-3 text-pink-500 transition-transform reasoning-chevron" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
                </svg>
            </button>
            <div class="reasoning-block">
                <div class="reasoning-content mt-1 ml-6 p-3 rounded-lg border border-pink-200 bg-pink-50 text-xs text-pink-900 leading-relaxed whitespace-pre-wrap"></div>
            </div>`;
        container.appendChild(div);
        return div;
    }

    function updateReasoningBlock(wrapper, text) {
        const content = wrapper.querySelector('.reasoning-content');
        const preview = wrapper.querySelector('.reasoning-preview');
        content.textContent = text;
        const firstLine = text.split('\n')[0] || '';
        preview.textContent = firstLine.slice(0, 80) + (firstLine.length > 80 ? '...' : '');
    }

    function collapseReasoningBlock(wrapper) {
        const block = wrapper.querySelector('.reasoning-block');
        const icon = wrapper.querySelector('.reasoning-icon');
        const label = wrapper.querySelector('.font-medium');
        block.classList.remove('open');
        icon.classList.remove('animate-pulse');
        if (label) label.textContent = 'Thought';
        wrapper.querySelector('.reasoning-chevron').style.transform = '';
    }

    function toggleReasoning(btn) {
        const wrapper = btn.closest('.reasoning-wrapper');
        const block = wrapper.querySelector('.reasoning-block');
        const chevron = wrapper.querySelector('.reasoning-chevron');
        block.classList.toggle('open');
        chevron.style.transform = block.classList.contains('open') ? 'rotate(90deg)' : '';
    }

    function eventTypeMatches(event, ...types) {
        return types.includes(event.event_type || event.type);
    }

    function eventTime(row) {
        const raw = row && row.created_at;
        if (!raw) return 0;
        const stamp = Date.parse(raw);
        return Number.isNaN(stamp) ? 0 : stamp;
    }

    /**
     * Replay one turn's persisted events into its assistant row.
     *
     * ``state.interactions`` maps a request id to its live card so a replayed
     * prompt and its stored outcome land on the same element. The caller owns
     * that map because it also keys live events; an absent map still renders
     * every card, just without later resolution.
     */
    function replaySessionEvents(assistantEl, events, interactions) {
        if (!events || !events.length) return;
        const cards = interactions || {};
        const toolsEl = assistantEl.querySelector('.tools-container');
        let reasoningEl = null;
        let reasoningText = '';

        for (const event of events) {
            if (eventTypeMatches(event, 'tool_call', 'tool.call')) {
                appendToolCall(toolsEl, event.tool_name || 'unknown', event.tool_input);
            } else if (eventTypeMatches(event, 'tool_result', 'tool.result')) {
                updateToolResult(toolsEl, event.tool_name || 'unknown', event.tool_output);
            } else if (eventTypeMatches(event, 'hook_context')) {
                appendHookCall(toolsEl, event);
            } else if (eventTypeMatches(event, 'subagent_report')) {
                appendSubagentReport(toolsEl, event);
            } else if (eventTypeMatches(event, 'reasoning')) {
                if (!reasoningEl) reasoningEl = createReasoningBlock(toolsEl);
                reasoningText += event.detail || '';
                updateReasoningBlock(reasoningEl, reasoningText);
            } else if (eventTypeMatches(event, 'tool_approval_request', 'user_question')) {
                const card = appendInteractionRequest(toolsEl, event);
                const info = interactionPayload(event);
                if (info.requestId) cards[info.requestId] = card;
            } else if (eventTypeMatches(event, 'tool_approval_result', 'user_question_result')) {
                const raw = interactionOutcome(event);
                const requestId = raw.request_id
                    || interactionPayload(event).requestId;
                const card = requestId ? cards[requestId] : null;
                if (card) {
                    resolveInteractionCard(card, raw || {});
                    delete cards[requestId];
                }
            }
        }

        if (reasoningEl) collapseReasoningBlock(reasoningEl);
        finalizeToolCalls(toolsEl);
    }

    /**
     * Render a whole persisted transcript into ``container``.
     *
     * Events are grouped by turn so a tool call renders inside the assistant
     * row it belongs to, exactly as it did live. Cards that could not get a
     * reply turn (a background report settling after the conversation went
     * idle) carry no turn id and used to be dropped; they are interleaved by
     * timestamp instead so the thread stays in the order it happened.
     *
     * Returns the rendered rows so a caller can scroll or measure afterwards.
     */
    function renderTranscript(container, messages, events, options) {
        const opts = options || {};
        container.innerHTML = '';
        const interactions = opts.interactions || {};
        const visible = (messages || []).filter(m => m.role === 'user' || m.role === 'assistant');
        const turnIdByMessageId = new Map(
            visible.filter(m => m.id && m.turn_id).map(m => [m.id, m.turn_id])
        );
        const assistantTurns = new Set(
            visible.filter(m => m.role === 'assistant' && m.turn_id).map(m => m.turn_id)
        );
        const eventsByTurn = new Map();
        const ORPHAN_TURN = '__delegated_report__';
        for (const event of events || []) {
            if (event.visible === false) continue;
            const isReport = eventTypeMatches(event, 'subagent_report');
            const turnId = event.turn_id
                || turnIdByMessageId.get(event.message_id)
                || (isReport ? ORPHAN_TURN : null);
            if (!turnId) continue;
            const turnEvents = eventsByTurn.get(turnId) || [];
            turnEvents.push(event);
            eventsByTurn.set(turnId, turnEvents);
        }
        for (const turnEvents of eventsByTurn.values()) {
            turnEvents.sort((left, right) => (left.sequence || 0) - (right.sequence || 0));
        }

        const rows = [];
        const orphanCards = (eventsByTurn.get(ORPHAN_TURN) || [])
            .slice()
            .sort((left, right) => eventTime(left) - eventTime(right));
        const flushOrphanCardsBefore = (stamp) => {
            const due = [];
            while (orphanCards.length && eventTime(orphanCards[0]) < stamp) {
                due.push(orphanCards.shift());
            }
            if (!due.length) return;
            const row = appendAssistantMessage(container, '');
            replaySessionEvents(row, due, interactions);
            rows.push(row);
        };

        for (const msg of visible) {
            flushOrphanCardsBefore(eventTime(msg));
            if (msg.role === 'user') {
                const att = msg.metadata && msg.metadata.attachments;
                rows.push(appendUserMessage(container, msg.content || '', att));
                const turnEvents = eventsByTurn.get(msg.turn_id) || [];
                // A turn that ended without an assistant message (aborted,
                // timed out, still running) still has visible events. Render
                // them so the thread never silently loses a tool call.
                if (turnEvents.length && !assistantTurns.has(msg.turn_id)) {
                    const row = appendAssistantMessage(container, '');
                    replaySessionEvents(row, turnEvents, interactions);
                    rows.push(row);
                }
            } else {
                const row = appendAssistantMessage(container, msg.content || '');
                replaySessionEvents(row, eventsByTurn.get(msg.turn_id) || [], interactions);
                const autoInfo = msg.metadata && msg.metadata.auto_selection;
                if (autoInfo) appendAutoSelectionNote(row, autoInfo);
                rows.push(row);
            }
        }

        if (orphanCards.length) {
            const row = appendAssistantMessage(container, '');
            replaySessionEvents(row, orphanCards, interactions);
            rows.push(row);
        }
        return rows;
    }

    function toggleToolDetails(toolId) {
        const details = document.getElementById(`details-${toolId}`);
        if (!details) return;
        const wrapper = details.closest('.tool-chip-wrapper');
        details.classList.toggle('open');
        wrapper.querySelector('.tool-chevron').style.transform = details.classList.contains('open')
            ? 'rotate(90deg)'
            : '';
    }

    // ═══════════════════════════════════════════════════════════════════
    // Conversation controller
    // ═══════════════════════════════════════════════════════════════════
    // Sending a turn, rendering the SSE stream, answering an inline question,
    // and stopping generation are identical on every surface that hosts a
    // Lucent conversation. They live here once so a Handoff cannot quietly
    // fall behind /chat — which is exactly what happened when the two had
    // separate copies: the Handoff learned to send but not to stream tool
    // calls, so its transcript dropped everything the assistant did.

    function createConversation(config) {
        const cfg = config || {};
        if (!cfg.messagesEl) throw new Error('createConversation requires a messagesEl');

        const state = {
            sessionId: cfg.sessionId || '',
            messages: [],
            streaming: false,
            abortController: null,
            modelsById: {},
            pendingAttachments: [],
            interactions: {},
            msgCount: 0,
        };
        const escape = escapeHtml;

        function el(id) {
            return document.getElementById(id);
        }

        function scrollToBottom() {
            cfg.messagesEl.scrollTop = cfg.messagesEl.scrollHeight;
        }

        function setStatus(text, streaming) {
            const bar = el('status-bar');
            const label = el('status-text');
            if (!bar || !label) return;
            if (!text) {
                bar.classList.add('hidden');
                return;
            }
            label.textContent = text;
            bar.classList.remove('hidden');
            const composer = el('chat-input');
            if (composer) composer.closest('.input-glow')?.classList.toggle('is-streaming', !!streaming);
        }

        function updateButtons() {
            const sendBtn = el('send-btn');
            const stopBtn = el('stop-btn');
            const input = el('chat-input');
            if (!sendBtn || !input) return;
            const hasText = !!input.value.trim() || !!state.pendingAttachments.length;
            sendBtn.disabled = state.streaming || !hasText;
            sendBtn.style.display = state.streaming ? 'none' : 'flex';
            if (stopBtn) {
                stopBtn.style.display = state.streaming ? 'flex' : 'none';
                stopBtn.classList.toggle('hidden', !state.streaming);
            }
            const composer = input.closest('.input-glow');
            if (composer) composer.classList.toggle('is-streaming', state.streaming);
        }

        function currentApprovalMode() {
            const select = el('tool-approval-select');
            return select && select.value ? select.value : 'auto';
        }

        function selectedModelSupportsVision() {
            const select = el('model-select');
            const model = select ? state.modelsById[select.value] : null;
            return !!(model && model.supports_vision);
        }

        // ── attachments ──────────────────────────────────────────────────
        const ATTACH_LIMITS = {
            maxCount: 8,
            image: { bytes: 5 * 1024 * 1024, mimes: ['image/png', 'image/jpeg', 'image/webp', 'image/gif'] },
            document: { bytes: 10 * 1024 * 1024, mimes: ['application/pdf', 'text/plain', 'text/markdown', 'text/csv'] },
        };

        function attachmentKindForMime(mime) {
            return ATTACH_LIMITS.image.mimes.includes(mime) ? 'image' : 'document';
        }

        function updateAttachAvailability() {
            const btn = el('attach-btn');
            if (!btn) return;
            // A closed conversation renders its composer read-only; loading the
            // model list must not quietly re-enable controls it disabled.
            if (cfg.readOnly) {
                btn.disabled = true;
                btn.title = 'This conversation is closed';
                return;
            }
            const supported = selectedModelSupportsVision();
            btn.disabled = !supported;
            btn.title = supported
                ? 'Attach images or documents'
                : 'The selected model does not support attachments';
            if (!supported && state.pendingAttachments.length) {
                state.pendingAttachments = [];
                renderAttachmentPreviews();
                updateButtons();
            }
        }

        function readFileAsAttachment(file) {
            return new Promise((resolve, reject) => {
                const allMimes = [...ATTACH_LIMITS.image.mimes, ...ATTACH_LIMITS.document.mimes];
                if (!allMimes.includes(file.type)) {
                    reject(new Error(`Unsupported file type: ${file.type || file.name}`));
                    return;
                }
                const kind = attachmentKindForMime(file.type);
                if (file.size > ATTACH_LIMITS[kind].bytes) {
                    const mb = Math.round(ATTACH_LIMITS[kind].bytes / (1024 * 1024));
                    reject(new Error(`${file.name} exceeds the ${mb} MB limit`));
                    return;
                }
                const reader = new FileReader();
                reader.onload = () => {
                    const dataUrl = String(reader.result || '');
                    const base64 = dataUrl.includes(',') ? dataUrl.slice(dataUrl.indexOf(',') + 1) : dataUrl;
                    resolve({
                        kind, mime_type: file.type, name: file.name, size: file.size, data: base64, dataUrl,
                    });
                };
                reader.onerror = () => reject(new Error(`Failed to read ${file.name}`));
                reader.readAsDataURL(file);
            });
        }

        async function addPendingFiles(fileList) {
            if (cfg.readOnly || !selectedModelSupportsVision()) return;
            const files = Array.from(fileList || []);
            for (const file of files) {
                if (state.pendingAttachments.length >= ATTACH_LIMITS.maxCount) {
                    setStatus(`Up to ${ATTACH_LIMITS.maxCount} attachments`);
                    setTimeout(() => setStatus(''), 1800);
                    break;
                }
                try {
                    state.pendingAttachments.push(await readFileAsAttachment(file));
                } catch (err) {
                    setStatus(err.message || 'Could not attach file');
                    setTimeout(() => setStatus(''), 2400);
                }
            }
            renderAttachmentPreviews();
            updateButtons();
        }

        function removePendingAttachment(index) {
            state.pendingAttachments.splice(index, 1);
            renderAttachmentPreviews();
            updateButtons();
        }

        function renderAttachmentPreviews() {
            const container = el('attachment-previews');
            if (!container) return;
            container.innerHTML = '';
            if (!state.pendingAttachments.length) {
                container.classList.add('hidden');
                return;
            }
            container.classList.remove('hidden');
            state.pendingAttachments.forEach((att, index) => {
                const chip = document.createElement('div');
                chip.className = 'relative flex items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 pl-2 pr-7 py-1.5 text-xs text-gray-700 max-w-[220px]';
                const thumb = att.kind === 'image'
                    ? `<img src="${att.dataUrl}" alt="${escape(att.name)}" class="w-8 h-8 rounded object-cover shrink-0" />`
                    : `<span class="w-8 h-8 rounded bg-gray-200 flex items-center justify-center shrink-0"><svg class="w-4 h-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" /></svg></span>`;
                chip.innerHTML = `${thumb}<span class="truncate">${escape(att.name)}</span>
                    <button type="button" data-remove-attachment="${index}" class="absolute top-1 right-1 w-4 h-4 rounded-full bg-gray-300 hover:bg-gray-400 text-white flex items-center justify-center" title="Remove">
                        <svg class="w-2.5 h-2.5" fill="none" viewBox="0 0 24 24" stroke-width="3" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
                    </button>`;
                container.appendChild(chip);
            });
        }

        function pendingAttachmentsForRequest() {
            return state.pendingAttachments.map(({ kind, mime_type, name, size, data }) => ({
                kind, mime_type, name, size, data,
            }));
        }

        // ── model / agent / reasoning controls ───────────────────────────
        function updateReasoningOptions() {
            const modelSelect = el('model-select');
            const reasoningSelect = el('reasoning-select');
            if (!modelSelect || !reasoningSelect) return;
            const model = state.modelsById[modelSelect.value];
            const efforts = (model && model.reasoning_efforts) || [];
            reasoningSelect.innerHTML = '<option value="">Reasoning: default</option>';
            if (!efforts.length) {
                reasoningSelect.classList.add('hidden');
                reasoningSelect.value = '';
                return;
            }
            for (const effort of efforts) {
                const opt = document.createElement('option');
                opt.value = effort;
                opt.textContent = 'Reasoning: ' + effort;
                reasoningSelect.appendChild(opt);
            }
            reasoningSelect.classList.remove('hidden');
            reasoningSelect.value = '';
        }

        function setSelectValueIfPresent(selectId, value) {
            const select = el(selectId);
            if (!select || !value) return false;
            const stringValue = String(value);
            const exists = Array.from(select.options).some(opt => opt.value === stringValue);
            if (!exists) return false;
            select.value = stringValue;
            return true;
        }

        function addMissingModelOption(modelId) {
            const select = el('model-select');
            if (!select || !modelId || state.modelsById[modelId]) return;
            const group = document.createElement('optgroup');
            group.label = 'Previous session';
            const opt = document.createElement('option');
            opt.value = modelId;
            opt.textContent = `${modelId} (previously used)`;
            group.appendChild(opt);
            select.appendChild(group);
            state.modelsById[modelId] = { id: modelId, reasoning_efforts: [] };
        }

        function applySessionConfig(session) {
            if (!session) return;
            if (session.model) {
                addMissingModelOption(session.model);
                if (setSelectValueIfPresent('model-select', session.model)) {
                    updateReasoningOptions();
                }
            }
            if (session.reasoning_effort) {
                setSelectValueIfPresent('reasoning-select', session.reasoning_effort);
            }
            if (session.agent_definition_id) {
                setSelectValueIfPresent('agent-select', session.agent_definition_id);
            } else if (el('agent-select')) {
                el('agent-select').value = '';
            }
            const savedApproval = session.metadata && session.metadata.tool_approval_mode;
            if (savedApproval) setSelectValueIfPresent('tool-approval-select', savedApproval);
            updateAttachAvailability();
            if (typeof cfg.onSessionConfig === 'function') cfg.onSessionConfig(session);
        }

        async function loadChatControls() {
            const modelSelect = el('model-select');
            if (modelSelect) {
                try {
                    const resp = await fetch('/api/chat/models');
                    const data = await resp.json();
                    modelSelect.innerHTML = '';
                    state.modelsById = {};
                    const byProvider = {};
                    if (data.auto_selection_enabled) {
                        state.modelsById['auto'] = {
                            id: 'auto', name: 'Auto', reasoning_efforts: [], supports_vision: true,
                        };
                        const autoGroup = document.createElement('optgroup');
                        autoGroup.label = 'Auto';
                        const autoOpt = document.createElement('option');
                        autoOpt.value = 'auto';
                        autoOpt.textContent = 'Auto — best model per message';
                        if (!data.default) autoOpt.selected = true;
                        autoGroup.appendChild(autoOpt);
                        modelSelect.appendChild(autoGroup);
                    }
                    for (const model of data.models || []) {
                        state.modelsById[model.id] = model;
                        (byProvider[model.provider] = byProvider[model.provider] || []).push(model);
                    }
                    for (const [provider, models] of Object.entries(byProvider)) {
                        const group = document.createElement('optgroup');
                        group.label = provider.charAt(0).toUpperCase() + provider.slice(1);
                        for (const model of models) {
                            const opt = document.createElement('option');
                            opt.value = model.id;
                            opt.textContent = model.name;
                            if (model.id === data.default) opt.selected = true;
                            group.appendChild(opt);
                        }
                        modelSelect.appendChild(group);
                    }
                    modelSelect.addEventListener('change', updateReasoningOptions);
                    modelSelect.addEventListener('change', updateAttachAvailability);
                    updateReasoningOptions();
                    updateAttachAvailability();
                } catch (_error) {
                    modelSelect.innerHTML = '<option value="">Unavailable</option>';
                }
            }
            const agentSelect = el('agent-select');
            if (agentSelect) {
                try {
                    const resp = await fetch('/api/chat/agents');
                    const agents = await resp.json();
                    for (const a of agents || []) {
                        const opt = document.createElement('option');
                        opt.value = a.id;
                        // Name only: a native select sizes itself to its widest
                        // option, and an inline description made one option ~590px
                        // and pushed the whole control row off-screen.
                        opt.textContent = a.name;
                        if (a.description) opt.title = a.description;
                        agentSelect.appendChild(opt);
                    }
                } catch (_error) {
                    // The default Lucent option remains available.
                }
            }
        }

        // ── interaction cards ────────────────────────────────────────────
        function upsertInteractionCard(container, payload) {
            const card = appendInteractionRequest(container, payload);
            const info = interactionPayload(payload);
            if (info.requestId) {
                state.interactions[info.requestId] = card;
                card.dataset.turnId = payload && payload.turn_id ? payload.turn_id : '';
            }
            return card;
        }

        function resolveInteraction(payload) {
            const raw = interactionOutcome(payload);
            const info = interactionPayload(payload);
            const requestId = raw.request_id || info.requestId;
            const card = requestId ? state.interactions[requestId] : null;
            if (!card) return;
            resolveInteractionCard(card, raw || {});
            delete state.interactions[requestId];
        }

        function collectQuestionAnswers(card) {
            const fields = [...card.querySelectorAll('[data-interaction-answer]')];
            return fields.map(field => {
                const value = field.value.trim();
                const index = field.dataset.interactionAnswer;
                const selected = card.querySelector(
                    `[data-interaction-question="${index}"].is-selected`
                );
                return {
                    answer: value,
                    choice: selected && selected.dataset.interactionChoice === value ? value : null,
                };
            });
        }

        async function submitInteraction(card, body) {
            const requestId = card.dataset.interactionId;
            if (!requestId) return;
            card.querySelectorAll('button, input').forEach(node => { node.disabled = true; });
            const csrfCookie = document.cookie.split('; ').find(c => c.startsWith('lucent_csrf='));
            try {
                const resp = await fetch(`/chat/interactions/${encodeURIComponent(requestId)}/resolve`, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {
                        'Content-Type': 'application/json',
                        ...(csrfCookie ? { 'X-CSRF-Token': csrfCookie.split('=')[1] } : {}),
                    },
                    body: JSON.stringify({ session_id: card.dataset.sessionId || state.sessionId, ...body }),
                });
                if (!resp.ok) {
                    // The waiting turn is gone (aborted, timed out, restarted).
                    // Render that honestly instead of leaving live controls.
                    resolveInteraction({ request_id: requestId, ...body, expired: true });
                }
            } catch (_error) {
                card.querySelectorAll('button, input').forEach(node => { node.disabled = false; });
            }
        }

        // ── streaming turn ───────────────────────────────────────────────
        function stopStreaming() {
            if (state.abortController) {
                state.abortController.abort();
                state.abortController = null;
            }
        }

        async function sendTurn(event) {
            if (event && event.preventDefault) event.preventDefault();
            if (cfg.readOnly) return;
            const input = el('chat-input');
            if (!input || state.streaming) return;
            const text = input.value.trim();
            const outgoingAttachments = state.pendingAttachments.slice();
            if (!text && !outgoingAttachments.length) return;

            if (typeof cfg.ensureSession === 'function') {
                try {
                    const sessionId = await cfg.ensureSession(text);
                    if (sessionId) state.sessionId = sessionId;
                } catch (err) {
                    console.warn('Session creation failed, continuing unfiled', err);
                }
            }

            const userMessage = { role: 'user', content: text };
            if (outgoingAttachments.length) {
                userMessage.attachments = pendingAttachmentsForRequest();
            }
            state.messages.push(userMessage);
            appendUserMessage(cfg.messagesEl, text, outgoingAttachments);
            scrollToBottom();
            input.value = '';
            input.style.height = 'auto';
            state.pendingAttachments = [];
            renderAttachmentPreviews();

            state.streaming = true;
            state.abortController = new AbortController();
            updateButtons();
            setStatus('Thinking...', true);

            const assistantEl = createAssistantMessage(cfg.messagesEl, {});
            scrollToBottom();
            const contentEl = assistantEl.querySelector('.assistant-content');
            const toolsEl = assistantEl.querySelector('.tools-container');
            let reasoningEl = null;
            let reasoningText = '';
            let streamedText = '';
            let gotDeltas = false;

            const collapseReasoning = () => {
                if (!reasoningEl) return;
                collapseReasoningBlock(reasoningEl);
                reasoningEl = null;
                reasoningText = '';
            };

            try {
                const modelSelect = el('model-select');
                const reasoningSelect = el('reasoning-select');
                const agentSelect = el('agent-select');
                const resp = await fetch('/api/chat/stream-v2', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        messages: state.messages.map((m, i) =>
                            i === state.messages.length - 1
                                ? m
                                : { role: m.role, content: m.content }
                        ),
                        session_id: state.sessionId,
                        surface: cfg.surface || 'chat',
                        model: modelSelect && modelSelect.value ? modelSelect.value : null,
                        reasoning_effort: reasoningSelect && reasoningSelect.value ? reasoningSelect.value : null,
                        agent_id: agentSelect && agentSelect.value ? agentSelect.value : null,
                        tool_approval_mode: currentApprovalMode(),
                    }),
                    signal: state.abortController.signal,
                });

                if (!resp.ok) {
                    const errText = await resp.text().catch(() => resp.statusText);
                    contentEl.innerHTML = resp.status === 401
                        ? '<div class="text-red-500 text-sm">Session expired. Please <a href="/login" class="underline">log in again</a>.</div>'
                        : `<div class="text-red-500 text-sm">Error ${resp.status}: ${escape(errText)}</div>`;
                    setStatus('', false);
                    state.streaming = false;
                    state.abortController = null;
                    updateButtons();
                    return;
                }

                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop() || '';
                    for (const line of lines) {
                        if (!line.startsWith('data: ')) continue;
                        let payload;
                        try { payload = JSON.parse(line.slice(6)); } catch (_err) { continue; }

                        switch (payload.type) {
                            case 'session':
                                state.sessionId = payload.session_id;
                                if (typeof cfg.onSessionStarted === 'function') {
                                    cfg.onSessionStarted(payload.session_id, payload);
                                }
                                refreshDelegatedWork();
                                if (payload.model_selected) {
                                    appendAutoSelectionNote(assistantEl, payload.model_selected);
                                }
                                break;
                            case 'text_delta':
                                gotDeltas = true;
                                streamedText += payload.text;
                                contentEl.innerHTML = renderMarkdown(streamedText);
                                contentEl.classList.add('streaming-cursor');
                                setStatus('Writing...', true);
                                scrollToBottom();
                                break;
                            case 'message':
                                if (!gotDeltas) {
                                    streamedText = payload.text;
                                    contentEl.innerHTML = renderMarkdown(payload.text);
                                }
                                contentEl.classList.remove('streaming-cursor');
                                setStatus('', false);
                                scrollToBottom();
                                break;
                            case 'reasoning':
                                if (!reasoningEl) reasoningEl = createReasoningBlock(toolsEl);
                                reasoningText += payload.text;
                                updateReasoningBlock(reasoningEl, reasoningText);
                                setStatus('Thinking...', true);
                                scrollToBottom();
                                break;
                            case 'tool_call':
                                appendToolCall(toolsEl, payload.tool, payload.input);
                                collapseReasoning();
                                setStatus(`Using ${displayToolName(payload.tool)}...`, true);
                                scrollToBottom();
                                break;
                            case 'tool_result':
                                updateToolResult(toolsEl, payload.tool, payload.output);
                                setStatus('Thinking...', true);
                                scrollToBottom();
                                break;
                            case 'hook_context':
                                appendHookCall(toolsEl, payload);
                                scrollToBottom();
                                break;
                            case 'subagent_report':
                                appendSubagentReport(toolsEl, payload);
                                scrollToBottom();
                                break;
                            case 'tool_approval_request':
                                upsertInteractionCard(toolsEl, payload);
                                setStatus(`Waiting for you to allow ${displayToolName(payload.tool)}...`, true);
                                scrollToBottom();
                                break;
                            case 'tool_approval_result':
                                resolveInteraction(payload);
                                setStatus(payload.decision === 'approve' ? 'Running…' : 'Thinking...', true);
                                scrollToBottom();
                                break;
                            case 'user_question':
                                upsertInteractionCard(toolsEl, payload);
                                setStatus('Waiting for your answer...', true);
                                scrollToBottom();
                                break;
                            case 'user_question_result':
                                resolveInteraction(payload);
                                setStatus('Thinking...', true);
                                scrollToBottom();
                                break;
                            case 'error':
                                contentEl.innerHTML = '<div class="flex items-center gap-2 text-red-600 text-sm"><span>' + escape(payload.error) + '</span></div>';
                                setStatus('', false);
                                break;
                            case 'done':
                                contentEl.classList.remove('streaming-cursor');
                                if (!gotDeltas && !streamedText) {
                                    contentEl.innerHTML = '<div class="text-gray-400 text-sm italic">No response received. The model may be unavailable — try again.</div>';
                                }
                                finalizeToolCalls(toolsEl);
                                collapseReasoning();
                                setStatus('', false);
                                break;
                        }
                    }
                }
            } catch (err) {
                if (err.name !== 'AbortError') {
                    contentEl.innerHTML = '<div class="text-red-500 text-sm">Connection error: ' + escape(String(err.message || err)) + '</div>';
                }
                setStatus('', false);
            }

            contentEl.classList.remove('streaming-cursor');
            if (streamedText) {
                state.messages.push({ role: 'assistant', content: streamedText });
            }
            state.streaming = false;
            state.abortController = null;
            updateButtons();
            refreshDelegatedWork();
            flushPendingResync();
            if (typeof cfg.onTurnComplete === 'function') cfg.onTurnComplete();
        }

        // ── wiring ───────────────────────────────────────────────────────
        function bind() {
            const form = document.querySelector('#conversation-root form')
                || (el('chat-input') ? el('chat-input').closest('form') : null);
            if (form) form.addEventListener('submit', sendTurn);

            const input = el('chat-input');
            if (input) {
                input.addEventListener('keydown', function (event) {
                    if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault();
                        sendTurn(event);
                    }
                    setTimeout(updateButtons, 0);
                });
                input.addEventListener('input', function () {
                    this.style.height = 'auto';
                    this.style.height = Math.min(this.scrollHeight, 200) + 'px';
                    updateButtons();
                });
                input.addEventListener('paste', function (event) {
                    if (!selectedModelSupportsVision()) return;
                    const items = (event.clipboardData && event.clipboardData.items) || [];
                    const files = [];
                    for (const item of items) {
                        if (item.kind === 'file') {
                            const file = item.getAsFile();
                            if (file) files.push(file);
                        }
                    }
                    if (files.length) {
                        event.preventDefault();
                        addPendingFiles(files);
                    }
                });
            }

            const stopBtn = el('stop-btn');
            if (stopBtn) stopBtn.addEventListener('click', stopStreaming);

            const attachBtn = el('attach-btn');
            const attachInput = el('attach-input');
            if (attachBtn && attachInput) {
                attachBtn.addEventListener('click', function () {
                    if (!attachBtn.disabled) attachInput.click();
                });
                attachInput.addEventListener('change', function () {
                    addPendingFiles(this.files);
                    this.value = '';
                });
            }

            const approvalSelect = el('tool-approval-select');
            if (approvalSelect && typeof cfg.persistApprovalMode === 'function') {
                approvalSelect.addEventListener('change', function () {
                    cfg.persistApprovalMode(this.value);
                });
            }

            // One delegated listener for every interaction card, tool chip, and
            // reasoning toggle: cards are created dynamically and re-created on
            // replay, so per-element handlers would leak and go stale.
            document.addEventListener('click', function (event) {
                const decisionBtn = event.target.closest('[data-interaction-decision]');
                if (decisionBtn) {
                    const card = decisionBtn.closest('.interaction-card');
                    if (!card) return;
                    const decision = decisionBtn.dataset.interactionDecision;
                    if (decision === 'approve' || decision === 'deny') {
                        const remember = decision === 'approve'
                            && !!card.querySelector('[data-interaction-remember]')?.checked;
                        const noteField = card.querySelector('[data-interaction-note]');
                        submitInteraction(card, {
                            decision,
                            remember,
                            note: noteField ? noteField.value.trim() : '',
                        });
                    } else if (decision === 'answer') {
                        const answers = collectQuestionAnswers(card);
                        if (!answers.some(entry => entry.answer)) {
                            const empty = [...card.querySelectorAll('[data-interaction-answer]')]
                                .find(field => !field.value.trim());
                            if (empty) empty.focus();
                            return;
                        }
                        submitInteraction(card, {
                            answer: answers.map(entry => entry.answer).filter(Boolean).join(' | '),
                            choice: null,
                            answers,
                        });
                    } else if (decision === 'skip') {
                        submitInteraction(card, { answer: '', choice: null, answers: [] });
                    }
                    return;
                }

                const choice = event.target.closest('[data-interaction-choice]');
                if (choice) {
                    const card = choice.closest('.interaction-card');
                    if (!card) return;
                    const value = choice.dataset.interactionChoice || '';
                    const index = parseInt(choice.dataset.interactionQuestion || '0', 10);
                    const answers = collectQuestionAnswers(card);
                    answers[index] = { answer: value, choice: value };
                    const field = card.querySelector(`[data-interaction-answer="${index}"]`);
                    if (field) field.value = value;
                    card.querySelectorAll(`[data-interaction-question="${index}"]`)
                        .forEach(node => node.classList.toggle('is-selected', node === choice));
                    const total = card.querySelectorAll('[data-interaction-question-block]').length;
                    const answered = answers.filter(entry => entry && entry.answer).length;
                    if (total <= 1 || answered >= total) {
                        submitInteraction(card, {
                            answer: answers.map(entry => entry && entry.answer).filter(Boolean).join(' | '),
                            choice: total <= 1 ? value : null,
                            answers,
                        });
                    }
                    return;
                }

                const toolBtn = event.target.closest('[data-tool-toggle]');
                if (toolBtn) {
                    toggleToolDetails(toolBtn.dataset.toolToggle);
                    return;
                }

                const reasoningBtn = event.target.closest('[data-reasoning-toggle]');
                if (reasoningBtn) {
                    toggleReasoning(reasoningBtn);
                    return;
                }

                const removeBtn = event.target.closest('[data-remove-attachment]');
                if (removeBtn) {
                    removePendingAttachment(parseInt(removeBtn.dataset.removeAttachment, 10));
                }
            });

            document.addEventListener('keydown', function (event) {
                if (event.key === 'Escape' && state.streaming) stopStreaming();
                if (event.key !== 'Enter') return;
                const field = event.target.closest('[data-interaction-answer], [data-interaction-note]');
                if (!field) return;
                const card = field.closest('.interaction-card');
                if (!card) return;
                event.preventDefault();
                if (field.hasAttribute('data-interaction-answer')) {
                    const answers = collectQuestionAnswers(card);
                    if (!answers.some(entry => entry.answer)) return;
                    submitInteraction(card, {
                        answer: answers.map(entry => entry.answer).filter(Boolean).join(' | '),
                        choice: null,
                        answers,
                    });
                } else {
                    const approve = card.querySelector('[data-interaction-decision="approve"]');
                    if (approve) approve.click();
                }
            });
        }

        function renderTranscript(messages, events) {
            state.messages = [];
            state.interactions = {};
            const visible = (messages || []).filter(m => m.role === 'user' || m.role === 'assistant');
            LucentChatMessageUI.renderTranscript(cfg.messagesEl, messages, events, {
                interactions: state.interactions,
            });
            visible.forEach(msg => {
                state.messages.push({ role: msg.role, content: msg.content || '' });
            });
            scrollToBottom();
            if (typeof cfg.afterRender === 'function') cfg.afterRender();
            return visible;
        }

        // ── delegated work ───────────────────────────────────────────────
        // The conversation is the supervisor of the work it delegated, so its
        // live progress belongs on whatever surface is showing that
        // conversation — including a Handoff Lucent opened to report back.
        const DELEGATED_OPEN_STATUSES = new Set([
            'pending', 'planned', 'in_progress', 'review', 'needs_rework', 'rejection_processing',
        ]);
        const delegated = {
            requests: [],
            revision: null,
            reportRevision: null,
            activeCount: 0,
            timer: null,
            resyncPending: false,
        };

        function renderDelegatedProgress(force) {
            const requests = delegated.requests || [];
            const live = requests.filter(
                r => DELEGATED_OPEN_STATUSES.has(String(r.status || ''))
            );
            if (force) {
                cfg.messagesEl
                    .querySelectorAll('.delegated-progress')
                    .forEach(node => node.remove());
            }
            live.forEach(request => upsertDelegatedProgress(cfg.messagesEl, request));
            pruneDelegatedProgress(cfg.messagesEl, live.map(r => r.id));
        }

        function scheduleDelegatedPoll() {
            if (delegated.timer) {
                window.clearTimeout(delegated.timer);
                delegated.timer = null;
            }
            if (!state.sessionId) return;
            // Tight while work is running (someone is watching), slow otherwise:
            // an idle conversation should not poll for nothing.
            const delay = delegated.activeCount > 0 ? 4000 : 25000;
            delegated.timer = window.setTimeout(() => refreshDelegatedWork(), delay);
        }

        async function refreshDelegatedWork() {
            if (!state.sessionId) {
                delegated.requests = [];
                delegated.revision = null;
                delegated.activeCount = 0;
                scheduleDelegatedPoll();
                return;
            }
            try {
                const resp = await fetch(
                    `/api/chat/sessions/${encodeURIComponent(state.sessionId)}/activity`,
                    { credentials: 'same-origin', cache: 'no-store' }
                );
                if (!resp.ok) return;
                const data = await resp.json();
                const previousRevision = delegated.revision;
                const previousReportRevision = delegated.reportRevision;
                delegated.requests = data.requests || [];
                delegated.revision = data.revision || null;
                delegated.reportRevision = data.report_revision || null;
                delegated.activeCount = data.active_task_count || 0;
                // First successful read has no baseline to diff against, so the
                // progress cards are (re)built from scratch and no reload fires.
                renderDelegatedProgress(previousRevision === null);
                if (typeof cfg.onDelegatedWork === 'function') {
                    cfg.onDelegatedWork(delegated);
                }
                // A new REPORT CARD is the one activity change that needs the
                // transcript re-read: the card and Lucent's unattended reply are
                // appended rows, not progress we can patch in place. Its own
                // revision token is used deliberately — the work revision ticks
                // on every task transition, and rebuilding the thread on those
                // reset scroll position while work was running.
                const reportChanged = previousReportRevision !== null
                    && previousReportRevision !== undefined
                    && previousReportRevision !== delegated.reportRevision;
                if (reportChanged) requestDelegatedResync();
            } catch (_error) {
                // Live updates are best-effort.
            } finally {
                scheduleDelegatedPoll();
            }
        }

        function requestDelegatedResync() {
            // Re-read the transcript so a newly written report card (and the
            // unattended reply that accompanies it) appears without a manual
            // reload. Deliberately gated on the report revision, not the work
            // revision: a running request bumps the latter constantly, and
            // rebuilding the thread every few seconds reset scroll position and
            // made the page visibly jump. Live *progress* is patched in place by
            // renderDelegatedProgress; only a new card needs the full re-read.
            if (!state.sessionId) return;
            if (state.streaming) {
                // Never rebuild the transcript mid-stream: it would throw away
                // the reply currently being written. Re-read once it settles.
                delegated.resyncPending = true;
                return;
            }
            delegated.resyncPending = false;
            if (typeof cfg.reloadTranscript === 'function') cfg.reloadTranscript();
        }

        /** Re-read the transcript after a turn ends if a report arrived mid-stream. */
        function flushPendingResync() {
            if (!delegated.resyncPending) return;
            delegated.resyncPending = false;
            if (typeof cfg.reloadTranscript === 'function') cfg.reloadTranscript();
        }

        if (typeof cfg.reloadTranscript === 'function') {
            document.addEventListener('lucent:live-refresh', () => refreshDelegatedWork());
        }

        return {
            state,
            addPendingFiles,
            applySessionConfig,
            bind,
            loadChatControls,
            flushPendingResync,
            refreshDelegatedWork,
            removePendingAttachment,
            requestDelegatedResync,
            renderAttachmentPreviews,
            renderTranscript,
            scrollToBottom,
            sendTurn,
            setSessionId(value) { state.sessionId = value || ''; },
            setStatus,
            stopStreaming,
            updateAttachAvailability,
            updateButtons,
            updateReasoningOptions,
            upsertInteractionCard,
        };
    }

    window.LucentChatMessageUI = {
        createConversation,
        appendHookCall,
        appendInteractionRequest,
        appendSubagentReport,
        upsertDelegatedProgress,
        pruneDelegatedProgress,
        appendToolCall,
        displayToolName,
        escapeHtml,
        finalizeToolCalls,
        formatJSON,
        interactionOutcome,
        interactionPayload,
        renderMarkdown,
        resolveInteractionCard,
        toggleToolDetails,
        updateToolResult,
        // Transcript API — one implementation for /chat and Handoff.
        appendAssistantMessage,
        appendAutoSelectionNote,
        appendUserMessage,
        collapseReasoningBlock,
        createAssistantMessage,
        createReasoningBlock,
        eventTime,
        eventTypeMatches,
        fillAssistantMessage,
        renderTranscript,
        replaySessionEvents,
        toggleReasoning,
        updateReasoningBlock,
    };
})();
