{{/* Expand the name of the chart. */}}
{{- define "lucent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Create a default fully qualified app name. */}}
{{- define "lucent.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Create chart name and version as used by the chart label. */}}
{{- define "lucent.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels */}}
{{- define "lucent.labels" -}}
helm.sh/chart: {{ include "lucent.chart" . }}
{{ include "lucent.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Selector labels */}}
{{- define "lucent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "lucent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Service account name */}}
{{- define "lucent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "lucent.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Daemon controller service account. The controller is a distinct, privileged
component (it can spawn Jobs in the worker namespace) and therefore gets its
own identity, never the server's sandbox-capable one.
*/}}
{{- define "lucent.daemonServiceAccountName" -}}
{{- if .Values.daemon.serviceAccount.create -}}
{{- default (printf "%s-daemon" (include "lucent.fullname" .)) .Values.daemon.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.daemon.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Namespace the controller spawns worker Jobs into. Named by
LUCENT_WORKER_NAMESPACE; defaults to the release namespace so no
cluster-scoped resource (Namespace) or out-of-band namespace is required.
Override daemon.workerNamespace to point workers at a dedicated namespace —
the controller Role/RoleBinding and the worker NetworkPolicy follow it.
*/}}
{{- define "lucent.workerNamespace" -}}
{{- default .Release.Namespace .Values.daemon.workerNamespace -}}
{{- end -}}

{{/*
Image the controller launches worker Jobs with (LUCENT_WORKER_IMAGE). One
image serves both roles, so it defaults to the server image; override
daemon.workerImage only when workers must track a different tag.
*/}}
{{- define "lucent.workerImage" -}}
{{- default (printf "%s:%s" .Values.server.image.repository .Values.server.image.tag) .Values.daemon.workerImage -}}
{{- end -}}

{{/*
MCP endpoint the daemon reaches the server over, in-cluster. Workers inherit
this (LUCENT_MCP_URL is on the worker env allowlist), so it must be a URL
reachable from the worker namespace, not localhost.
*/}}
{{- define "lucent.daemonMcpUrl" -}}
{{- printf "http://%s-server.%s.svc:%v/mcp" (include "lucent.fullname" .) .Release.Namespace .Values.server.service.port -}}
{{- end -}}
