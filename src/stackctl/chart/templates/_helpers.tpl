{{- define "stackctl.labels" -}}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/part-of: {{ .root.Values.stack }}
app.kubernetes.io/managed-by: stackctl
{{- end }}

{{- define "stackctl.selector" -}}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/name: {{ .name }}
{{- end }}

{{/* env, envFrom and secret-file mounts shared by containers, init containers and jobs */}}
{{- define "stackctl.containerEnv" -}}
{{- with .item.env }}
env:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- if .item.secretEnv }}
envFrom:
  - secretRef:
      name: {{ .root.Release.Name }}-env
{{- end }}
{{- end }}

{{- define "stackctl.mounts" -}}
{{- $root := .root }}
{{- if or (and .item.secretFiles $root.Values.secretFiles) .item.volumes }}
volumeMounts:
  {{- if and .item.secretFiles $root.Values.secretFiles }}
  {{- range $file, $_ := $root.Values.secretFiles }}
  - name: secret-files
    mountPath: {{ $root.Values.filesDir }}/{{ $file }}
    subPath: {{ $file }}
    readOnly: true
  {{- end }}
  {{- end }}
  {{- range .item.volumes }}
  - name: {{ .name }}
    mountPath: {{ .mount }}
  {{- end }}
{{- end }}
{{- end }}
