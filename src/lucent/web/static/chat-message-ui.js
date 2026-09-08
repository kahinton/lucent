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
                    <svg class="w-4 h-4 text-amber-500 animate-pulse" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M11.42 15.17 17.25 21A2.652 2.652 0 0 0 21 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766m0 0a4.5 4.5 0 0 0 6.229-6.476l-3.276 3.277a3.004 3.004 0 0 1-2.25-2.25l3.276-3.276a4.5 4.5 0 0 0-6.336 4.486c.049.58.025 1.194-.14 1.743Z" /></svg>
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

        return {
            hookName: asString(pick('hook')) || 'hook',
            phase: asString(pick('phase')),
            triggerTool: asString(pick('trigger_tool')),
            decision: asString(pick('decision')) || 'inject',
            fileRefs,
            memories,
            memoryCount,
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
        const decisionBlock = hook.decision && hook.decision !== 'inject'
            ? `<div class="hook-detail-row">${detailLabel('Decision')}<span class="hook-detail-value hook-decision hook-decision-${escapeHtml(hook.decision)}">${escapeHtml(hook.decision)}</span></div>`
            : '';
        const refsBlock = hook.fileRefs.length
            ? `<div class="hook-detail-row hook-detail-column">${detailLabel('Matched files')}<div class="hook-ref-list">${hook.fileRefs.map(ref => `<span class="hook-file-ref">${escapeHtml(ref)}</span>`).join('')}</div></div>`
            : '';
        const memoryItems = hook.memories.map(memory => {
            const tags = (memory.tags || []).map(tag => `<span class="hook-memory-tag">${escapeHtml(tag)}</span>`).join('');
            return `<li class="hook-memory-item"><span class="hook-memory-id">${escapeHtml(memory.id || '')}</span>${tags ? `<span class="hook-memory-tags">${tags}</span>` : ''}<span class="hook-memory-content">${escapeHtml(memory.content || '')}</span></li>`;
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
                    ${triggerBlock}${phaseBlock}${decisionBlock}${refsBlock}${memoriesBlock}${contextBlock}
                </div>
            </div>`;
        container.appendChild(wrapper);
        return wrapper;
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
        appendToolCall,
        displayToolName,
        escapeHtml,
        finalizeToolCalls,
        formatJSON,
        renderMarkdown,
        toggleToolDetails,
        updateToolResult,
    };
})();
