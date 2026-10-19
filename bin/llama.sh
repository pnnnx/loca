#!/bin/sh
# Запуск llama-server под loca (Mac на 8 ГБ).
#
#   bin/llama.sh                                   # qwen3.5:4b с Hugging Face (скачается один раз)
#   bin/llama.sh ~/models/my-coder-v2-Q4_K_M.gguf   # свой файл
#   bin/llama.sh unsloth/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M
#   LOCA_CTX=8192 bin/llama.sh ...                 # меньше контекст — меньше памяти
#
# Потом в другом терминале: loca --backend llamacpp   (или "backend": "llamacpp" в ~/.loca/config.json)
#
# Флаги:
#   --jinja                 шаблон чата модели → работают вызовы инструментов
#   -np 1                   один слот: иначе контекст делится на несколько параллельных запросов
#   -fa on -ctk/-ctv q8_0   flash attention и 8-битный KV-кэш — примерно вдвое меньше памяти на контекст
#   --reasoning-format deepseek  размышления qwen3 идут отдельно от ответа
MODEL="${1:-${LOCA_GGUF:-unsloth/Qwen3.5-4B-GGUF:Q4_K_M}}"
[ $# -gt 0 ] && shift
CTX="${LOCA_CTX:-16384}"
PORT="${LOCA_PORT:-8080}"
if [ -f "$MODEL" ]; then
  set -- -m "$MODEL" "$@"
else
  set -- -hf "$MODEL" "$@"
fi
exec llama-server "$@" --jinja -c "$CTX" -np 1 -fa on -ctk q8_0 -ctv q8_0 -ngl 99 \
  --reasoning-format deepseek --host 127.0.0.1 --port "$PORT"
