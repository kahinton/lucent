/*
 * Shared decision-step editor wiring for the workflow builder.
 * Used by workflow_new.html (create) and schedule_detail.html (edit):
 * each page wires its step cards through lucentDecisionUI.wireStep and
 * calls syncDecision whenever the step's agent select changes.
 */

(function () {
    'use strict';

    var TYPE_CONFIG = {
        noul: {
            hint: 'Probability (0–100%) that the condition in the instructions is true.',
            criteriaHidden: true,
            minHidden: false,
        },
        score: {
            hint: 'Rates the work against an ordered scale — the answer is a scale position.',
            criteriaHidden: false,
            minHidden: false,
        },
        choice: {
            hint: 'Pick exactly one option from a list — classification output; no threshold.',
            criteriaHidden: false,
            minHidden: true,
        },
    };

    function templateRow(templateId) {
        var tpl = document.getElementById(templateId);
        return tpl ? tpl.content.querySelector('.decision-question-row') : null;
    }

    function questionRows(root) {
        return Array.prototype.slice.call(root.querySelectorAll('.decision-question-row'));
    }

    function styleButtons(scope) {
        scope.querySelectorAll('button:disabled').forEach(function (button) {
            button.classList.add('cursor-not-allowed', 'opacity-30');
        });
        scope.querySelectorAll('button:not(:disabled)').forEach(function (button) {
            button.classList.remove('cursor-not-allowed', 'opacity-30');
        });
    }

    function syncCount(root) {
        var counter = root.querySelector('input[name="action_decision_q_count"]');
        if (counter) counter.value = String(questionRows(root).length);
    }

    function applyType(row) {
        var select = row.querySelector('.decision-question-type');
        if (!select) return;
        var cfg = TYPE_CONFIG[select.value] || TYPE_CONFIG.noul;
        var hint = row.querySelector('.decision-type-hint');
        if (hint) hint.textContent = cfg.hint;
        var criteriaWrap = row.querySelector('.decision-criteria-field');
        if (criteriaWrap) {
            criteriaWrap.classList.toggle('hidden', cfg.criteriaHidden);
        }
        var minWrap = row.querySelector('.decision-min-field');
        if (minWrap) {
            minWrap.classList.toggle('hidden', cfg.minHidden);
            if (cfg.minHidden) {
                var minInput = row.querySelector('input[name="decision_q_min"]');
                if (minInput) minInput.value = '';
            }
        }
    }

    function refreshQuestionLabels(root) {
        var rows = questionRows(root);
        rows.forEach(function (row, index) {
            var badge = row.querySelector('.decision-question-index');
            if (badge) badge.textContent = 'Question ' + (index + 1);
            var up = row.querySelector('.move-decision-question-up');
            var down = row.querySelector('.move-decision-question-down');
            var remove = row.querySelector('.remove-decision-question');
            if (up) up.disabled = index === 0;
            if (down) down.disabled = index === rows.length - 1;
            if (remove) remove.disabled = rows.length === 1;
            styleButtons(row);
        });
        syncCount(root);
    }

    function wireQuestionRow(root, row, opts) {
        if (row.__decisionWired) return;
        row.__decisionWired = true;
        var up = row.querySelector('.move-decision-question-up');
        var down = row.querySelector('.move-decision-question-down');
        var remove = row.querySelector('.remove-decision-question');
        var container = row.closest('.decision-questions');
        if (up) up.addEventListener('click', function () {
            var previous = row.previousElementSibling;
            if (container && previous && previous.matches('.decision-question-row')) {
                container.insertBefore(row, previous);
            }
            refreshQuestionLabels(root);
            if (opts.onDirty) opts.onDirty();
        });
        if (down) down.addEventListener('click', function () {
            var next = row.nextElementSibling;
            if (container && next && next.matches('.decision-question-row')) {
                container.insertBefore(next, row);
            }
            refreshQuestionLabels(root);
            if (opts.onDirty) opts.onDirty();
        });
        if (remove) remove.addEventListener('click', function () {
            if (questionRows(root).length === 1) return;
            row.remove();
            refreshQuestionLabels(root);
            if (opts.onDirty) opts.onDirty();
        });
        var typeSelect = row.querySelector('.decision-question-type');
        if (typeSelect) typeSelect.addEventListener('change', function () {
            applyType(row);
            if (opts.onDirty) opts.onDirty();
        });
        applyType(row);
    }

    function addQuestion(root, opts) {
        var container = root.querySelector('.decision-questions');
        var source = templateRow(opts.templateId);
        if (!container || !source) return;
        var row = source.cloneNode(true);
        container.appendChild(row);
        wireQuestionRow(root, row, opts);
        refreshQuestionLabels(root);
        if (opts.onDirty) opts.onDirty();
        var nameInput = row.querySelector('input[name="decision_q_name"]');
        if (nameInput) nameInput.focus();
    }

    function fullModelOptions(model, opts) {
        if (!model.__fullOptions) {
            model.__fullOptions = opts.modelOptionsSeed
                ? opts.modelOptionsSeed
                : Array.prototype.slice.call(model.options).map(function (option) {
                    return option.cloneNode(true);
                });
        }
        return model.__fullOptions;
    }

    function setModelOptions(root, opts, decisionMode) {
        var model = root.querySelector(opts.modelSelector);
        if (!model) return;
        var all = fullModelOptions(model, opts);
        var previous = model.value;
        model.innerHTML = '';
        var options = decisionMode
            ? all.filter(function (option) { return (option.dataset.modelClass || '') === 'decision'; })
            : all;
        if (decisionMode) {
            var fallback = document.createElement('option');
            fallback.value = '';
            fallback.textContent = 'Daemon decision default (resolves at dispatch)';
            options = [fallback].concat(options);
        }
        options.forEach(function (option) { model.appendChild(option.cloneNode(true)); });
        var restored = false;
        if (previous) {
            for (var i = 0; i < model.options.length; i++) {
                if (model.options[i].value === previous) {
                    model.value = previous;
                    restored = true;
                    break;
                }
            }
        }
        if (!restored) model.value = model.options.length ? model.options[0].value : '';
        model.__decisionSwapped = decisionMode;
        if (opts.onModelSwap) opts.onModelSwap(root);
    }

    function applyView(root, isDecision, opts) {
        // Segmented kind toggle + which parts of the card each mode shows.
        var toggle = root.querySelector('.step-kind-toggle');
        if (toggle) {
            toggle.querySelectorAll('button[data-kind]').forEach(function (button) {
                var active = (button.dataset.kind === 'decision') === isDecision;
                button.classList.toggle('bg-white', active);
                button.classList.toggle('text-gray-900', active);
                button.classList.toggle('shadow-sm', active);
                button.classList.toggle('text-gray-500', !active);
            });
        }
        var hint = root.querySelector('.step-kind-hint');
        if (hint) {
            hint.textContent = isDecision
                ? 'No agent session — a local decision model answers the questions below against the request and completed steps.'
                : 'Runs the selected agent in a session — instructions, sandbox, and structured output apply.';
        }
        root.querySelectorAll('.step-agent-only').forEach(function (el) {
            el.classList.toggle('hidden', isDecision);
        });
        var prompt = root.querySelector('textarea[name="action_prompt"]');
        if (prompt) prompt.required = !isDecision;
        var modelLabel = root.querySelector('.step-model-label');
        if (modelLabel) {
            modelLabel.textContent = isDecision ? 'Decision model' : 'Model override';
        }
    }

    function setAgentValue(agent, value) {
        for (var i = 0; i < agent.options.length; i++) {
            if (agent.options[i].value === value) {
                agent.selectedIndex = i;
                return true;
            }
        }
        return false;
    }

    function syncDecision(root, opts) {
        var agent = root.querySelector(opts.agentSelector);
        var isDecision = agent ? agent.value === 'decision-step' : false;
        var details = root.querySelector('.decision-step-details');
        if (details) {
            details.hidden = !isDecision;
            if (isDecision) details.open = true;
        }
        var mode = root.querySelector('input[name="action_decision_mode"]');
        if (mode) mode.value = isDecision ? 'builder' : '';
        applyView(root, isDecision, opts);
        var model = root.querySelector(opts.modelSelector);
        if (!model) return;
        var swapped = !!model.__decisionSwapped;
        if (isDecision && !swapped) setModelOptions(root, opts, true);
        else if (!isDecision && swapped) setModelOptions(root, opts, false);
    }

    function setKind(root, kind, opts) {
        var agent = root.querySelector(opts.agentSelector);
        if (!agent) return;
        if (kind === 'decision') {
            if (agent.value !== 'decision-step') {
                root.__lastAgent = agent.value;
                if (!setAgentValue(agent, 'decision-step')) return;
            }
            syncDecision(root, opts);
            if (opts.onAgentChange) opts.onAgentChange(root);
        } else {
            if (agent.value === 'decision-step') {
                // Keep the question rows in the DOM (hidden) so an accidental
                // toggle back and forth does not destroy the user's work; saving
                // with a non-decision agent still fails loudly with the rows' reason.
                var restored = false;
                if (root.__lastAgent) {
                    restored = setAgentValue(agent, root.__lastAgent);
                }
                if (!restored) {
                    for (var i = 0; i < agent.options.length; i++) {
                        if (agent.options[i].value !== 'decision-step') {
                            agent.selectedIndex = i;
                            break;
                        }
                    }
                }
                root.__lastAgent = null;
            }
            syncDecision(root, opts);
            if (opts.onAgentChange) opts.onAgentChange(root);
        }
    }

    function resetDecision(root, opts) {
        questionRows(root).forEach(function (row) { row.remove(); });
        var container = root.querySelector('.decision-questions');
        var source = templateRow(opts.templateId);
        if (container && source) {
            var row = source.cloneNode(true);
            container.appendChild(row);
            wireQuestionRow(root, row, opts);
        }
        var onFail = root.querySelector('select[name="action_decision_on_fail"]');
        if (onFail) onFail.value = '';
        var state = root.querySelector('textarea[name="action_decision_state_json"]');
        if (state) state.value = '';
        var raw = root.querySelector('textarea[name="action_decision_config_json"]');
        if (raw) raw.value = '';
        var mode = root.querySelector('input[name="action_decision_mode"]');
        if (mode) mode.value = '';
        var details = root.querySelector('.decision-step-details');
        if (details) details.hidden = true;
        var model = root.querySelector(opts.modelSelector);
        if (model) setModelOptions(root, opts, false);
        refreshQuestionLabels(root);
    }

    function wireStep(root, opts) {
        questionRows(root).forEach(function (row) { wireQuestionRow(root, row, opts); });
        var addButton = root.querySelector('.add-decision-question');
        if (addButton) addButton.addEventListener('click', function () { addQuestion(root, opts); });
        var agent = root.querySelector(opts.agentSelector);
        var hasDecisionAgent = agent && Array.prototype.some.call(agent.options, function (option) {
            return option.value === 'decision-step';
        });
        root.querySelectorAll('.step-kind-toggle button[data-kind]').forEach(function (button) {
            if (button.dataset.kind === 'decision' && !hasDecisionAgent) {
                button.disabled = true;
                button.title = 'No decision-step agent is available';
                button.classList.add('cursor-not-allowed', 'opacity-30');
                return;
            }
            button.addEventListener('click', function () {
                setKind(root, button.dataset.kind, opts);
                if (opts.onDirty) opts.onDirty();
            });
        });
        syncDecision(root, opts);
        refreshQuestionLabels(root);
    }

    window.lucentDecisionUI = {
        wireStep: wireStep,
        syncDecision: syncDecision,
        resetDecision: resetDecision,
        addQuestion: addQuestion,
    };
})();