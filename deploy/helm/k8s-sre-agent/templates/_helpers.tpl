{{- define "agent.name" -}}k8s-sre-agent{{- end -}}
{{- define "agent.labels" -}}
app.kubernetes.io/name: {{ include "agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "agent.serviceAccountName" -}}
{{- .Values.serviceAccount.name | default (include "agent.name" .) -}}
{{- end -}}
