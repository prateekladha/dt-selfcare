{{- define "selfcare.labels" -}}
app.kubernetes.io/part-of: selfcare
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{- define "selfcare.image" -}}
{{ .root.Values.image.registry }}/{{ .name }}:{{ .root.Values.image.tag }}
{{- end }}

{{/* Environment shared by every Python workload; secrets come from the Vault-synced Secret. */}}
{{- define "selfcare.env" -}}
- name: APP_VALKEY_URL
  value: redis://valkey:6379/0
- name: APP_STREAM_PARTITIONS
  value: {{ .Values.stream.partitions | quote }}
- name: STREAM_BOOTSTRAP
  value: {{ .Values.stream.bootstrap | quote }}
- name: STREAM_USAGE_TOPIC
  value: {{ .Values.stream.usageTopic | quote }}
- name: STREAM_ALERTS_TOPIC
  value: {{ .Values.stream.alertsTopic | quote }}
- name: STREAM_PARTITIONS
  value: {{ .Values.stream.partitions | quote }}
- name: STREAM_USERNAME
  valueFrom: { secretKeyRef: { name: selfcare-secrets, key: stream-username } }
- name: STREAM_PASSWORD
  valueFrom: { secretKeyRef: { name: selfcare-secrets, key: stream-password } }
- name: FLAGD_HOST
  value: flagd
- name: FLAGD_PORT
  value: "8013"
- name: APP_DB_DSN
  valueFrom: { secretKeyRef: { name: selfcare-secrets, key: db-dsn } }
- name: APP_DB_PASSWORD
  valueFrom: { secretKeyRef: { name: selfcare-secrets, key: db-app-password } }
- name: APP_JWT_PUBLIC_KEY   # verify-only; the private key goes to core-api alone (workloads.yaml)
  valueFrom: { secretKeyRef: { name: selfcare-secrets, key: jwt-public-key } }
- { name: APP_REQUIRE_JWT_KEYS, value: "true" }
- name: APP_DEMO_PASSWORD
  valueFrom: { secretKeyRef: { name: selfcare-secrets, key: demo-password } }
- name: APP_SEED_DEMO
  value: {{ .Values.demo.seed | quote }}
- name: APP_ORDER_SOURCE_TOKEN   # optional: dedicated source for add-on events (core-api sends, collector accepts)
  valueFrom: { secretKeyRef: { name: selfcare-order-source-token, key: value, optional: true } }
- name: APP_SOURCE_TOKENS
  valueFrom: { secretKeyRef: { name: selfcare-secrets, key: collector-source-tokens } }
- name: APP_ARCHIVE_BUCKET
  value: {{ .Values.oci.rawBucket | quote }}
- name: APP_ARCHIVE_NAMESPACE
  value: {{ .Values.oci.namespace | quote }}
- name: APP_PUBLIC_BASE_URL
  value: "https://{{ .Values.domain }}"
- name: APP_RAZORPAY_KEY_ID
  valueFrom: { secretKeyRef: { name: selfcare-razorpay-key-id, key: value, optional: true } }
- name: APP_RAZORPAY_KEY_SECRET
  valueFrom: { secretKeyRef: { name: selfcare-razorpay-key-secret, key: value, optional: true } }
- name: APP_RAZORPAY_WEBHOOK_SECRET
  valueFrom: { secretKeyRef: { name: selfcare-razorpay-webhook-secret, key: value, optional: true } }
{{- if .Values.llmGateway.enabled }}
- { name: APP_LLM_GATEWAY_URL, value: "http://llm-gateway:4000" }
- name: APP_LLM_GATEWAY_KEY
  valueFrom: { secretKeyRef: { name: selfcare-secrets, key: llm-gateway-key } }
{{- end }}
- { name: TEMPORAL_ADDRESS, value: "temporal:7233" }
- { name: APP_COLLECTOR_URL, value: "http://usage-collector:8080" }
- name: APP_SOURCE_TOKEN
  valueFrom: { secretKeyRef: { name: selfcare-secrets, key: source-token } }
- name: LANGFUSE_PUBLIC_KEY
  valueFrom: { secretKeyRef: { name: selfcare-langfuse-public-key, key: value, optional: true } }
- name: LANGFUSE_SECRET_KEY
  valueFrom: { secretKeyRef: { name: selfcare-langfuse-secret-key, key: value, optional: true } }
- { name: LANGFUSE_HOST, value: {{ .Values.llmGateway.langfuseHost | quote }} }
- name: APP_DEEPSEEK_API_KEY
  valueFrom: { secretKeyRef: { name: selfcare-deepseek-api-key, key: value, optional: true } }
- name: APP_MISTRAL_API_KEY
  valueFrom: { secretKeyRef: { name: selfcare-mistral-api-key, key: value, optional: true } }
{{- end }}

{{- define "selfcare.podSecurity" -}}
enableServiceLinks: false   # no docker-link env vars (e.g. FLAGD_PORT=tcp://...) leaking into config
securityContext:
  runAsNonRoot: true
  seccompProfile: { type: RuntimeDefault }
imagePullSecrets:
  - name: ocir-pull
{{- end }}

{{- define "selfcare.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: { drop: ["ALL"] }
{{- end }}
