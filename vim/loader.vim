" loca — агент для кода поверх Ollama. Подключает плагин из репозитория,
" чтобы обновления в ~/Documents/loca/vim подхватывались без копирования.
" Команды: :Loca [запрос] · :Loca! (--yolo) · :'<,'>Loca правь это
"          :LocaAsk вопрос · :LocaTest · :LocaResume
" Настройки: g:loca_cmd, g:loca_args, g:loca_split, g:loca_size, g:loca_no_mappings
" Файл создан автоматически; удалите его, чтобы отключить loca в Vim.

let s:dir = expand('~/Documents/loca/vim')
let s:plugin = s:dir . '/plugin/loca.vim'
if filereadable(s:plugin)
  if index(split(&runtimepath, ','), s:dir) < 0
    let &runtimepath = &runtimepath . ',' . s:dir
  endif
  execute 'source ' . fnameescape(s:plugin)
endif
