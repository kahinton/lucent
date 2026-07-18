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
