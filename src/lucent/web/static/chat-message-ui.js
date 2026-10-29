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
        review: { bar: 'bg-pink-500', chip: 'bg-pink-100 text-pink-700 border-pink-200', label: 'Ready for review' },
        muted: { bar: 'bg-gray-400', chip: 'bg-gray-100 text-gray-600 border-gray-200', label: 'Stopped' },
        info: { bar: 'bg-primary-500', chip: 'bg-primary-50 text-primary-700 border-primary-200', label: 'Update' },
    };

    function reportAccent(severity) {
        return REPORT_ACCENTS[severity] || REPORT_ACCENTS.info;
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

    function toggleToolDetails(toolId) {
        const details = document.getElementById(`details-${toolId}`);
        if (!details) return;
        const wrapper = details.closest('.tool-chip-wrapper');
        details.classList.toggle('open');
        wrapper.querySelector('.tool-chevron').style.transform = details.classList.contains('open')
            ? 'rotate(90deg)'
            : '';
    }

    window.LucentChatMessageUI = {
        appendHookCall,
        appendInteractionRequest,
        appendSubagentReport,
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
    };
})();
