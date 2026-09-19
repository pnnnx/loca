" loca.vim — вызов агента loca из Vim / Neovim.
"
" Установка (Vim 8 / Neovim):
"   set runtimepath+=~/Downloads/loca/vim
" или через менеджер плагинов, указав папку loca/vim.
"
" Команды:
"   :Loca [запрос]        диалог с агентом в терминале, cwd = корень проекта
"   :Loca! [запрос]       то же, но без запросов разрешений (--yolo)
"   :'<,'>Loca запрос     то же, но с ссылкой на выделенные строки (path:12-30)
"   :LocaAsk запрос       быстрый ответ в отдельном буфере, без правок (--mode plan)
"   :LocaTest [цель]      «прогони тесты и исправь падения»
"   :LocaResume           продолжить последнюю сессию в этой папке
"   :LocaRoot             показать, какой каталог считается корнем проекта
"
" Настройки:
"   g:loca_cmd        'loca' (можно 'python3 -m loca' или полный путь)
"   g:loca_args       ['--think'] — аргументы, добавляемые всегда
"   g:loca_split      'botright vertical' | 'botright' | 'tab' | 'float' (nvim)
"   g:loca_size       ширина/высота окна терминала (по умолчанию 0.4 экрана)
"   g:loca_no_mappings  1 — не создавать <leader>l… привязки
"   g:loca_root       принудительный корень проекта (иначе определяется по маркерам)

if exists('g:loaded_loca')
  finish
endif
let g:loaded_loca = 1

let s:has_term = has('nvim') || (has('terminal') && exists('*term_start'))

function! s:err(msg) abort
  echohl ErrorMsg | echomsg '[loca] ' . a:msg | echohl None
endfunction

function! s:argv(extra, prompt) abort
  let l:argv = split(get(g:, 'loca_cmd', 'loca'))
  let l:argv += get(g:, 'loca_args', [])
  let l:argv += a:extra
  if !empty(a:prompt)
    let l:argv += [a:prompt]
  endif
  return l:argv
endfunction

" Корень проекта: вверх по дереву до .git / .loca / манифеста, но НИКОГДА не выше
" домашнего каталога — иначе глобальный ~/.loca делал корнем весь home.
let s:root_dirs = ['.git', '.loca', '.hg', '.svn']
let s:root_files = ['pyproject.toml', 'setup.py', 'package.json', 'Cargo.toml', 'go.mod',
      \ 'Makefile', 'LOCA.md', 'AGENTS.md', 'CLAUDE.md']

function! s:clean(path) abort
  return substitute(resolve(fnamemodify(a:path, ':p')), '/\+$', '', '')
endfunction

" Каталоги от текущего файла вверх, не включая домашний и корень файловой системы.
function! s:chain(dir) abort
  let l:home = s:clean(expand('~'))
  let l:cur = s:clean(a:dir)
  let l:out = []
  while !empty(l:cur) && l:cur !=# '/' && l:cur !=# l:home
    call add(l:out, l:cur)
    let l:parent = fnamemodify(l:cur, ':h')
    if l:parent ==# l:cur
      break
    endif
    let l:cur = l:parent
  endwhile
  return l:out
endfunction

function! s:root() abort
  if !empty(get(g:, 'loca_root', ''))
    return s:clean(g:loca_root)
  endif
  let l:dir = ''
  if !empty(bufname('%')) && &buftype ==# ''
    let l:dir = expand('%:p:h')
  endif
  if empty(l:dir) || !isdirectory(l:dir)
    " терминал loca или буфер без файла: не терять уже найденный проект
    if !empty(get(s:, 'last_root', '')) && isdirectory(s:last_root)
      return s:last_root
    endif
    let l:dir = getcwd()
  endif
  let l:chain = s:chain(l:dir)
  if empty(l:chain)
    return s:clean(l:dir)
  endif
  " ближайший каталог с .git / .loca — это и есть проект
  for l:cand in l:chain
    for l:marker in s:root_dirs
      if isdirectory(l:cand . '/' . l:marker)
        let s:last_root = l:cand
        return l:cand
      endif
    endfor
  endfor
  " иначе самый верхний каталог с манифестом (корень проекта, а не подпакета)
  let l:best = ''
  for l:cand in l:chain
    for l:marker in s:root_files
      if filereadable(l:cand . '/' . l:marker)
        let l:best = l:cand
        break
      endif
    endfor
  endfor
  let s:last_root = empty(l:best) ? s:clean(l:dir) : l:best
  return s:last_root
endfunction

function! s:relpath(root) abort
  let l:abs = expand('%:p')
  if empty(l:abs)
    return ''
  endif
  let l:root = substitute(a:root, '/$', '', '')
  if strpart(l:abs, 0, len(l:root) + 1) ==# l:root . '/'
    return strpart(l:abs, len(l:root) + 1)
  endif
  return l:abs
endfunction

" Запрос с ссылкой на выделенные строки — агент прочитает их сам через read_file.
" a:range — сколько частей диапазона дал пользователь (0 = ни одной)
function! s:with_ref(prompt, root, range, line1, line2) abort
  if a:range == 0
    return a:prompt
  endif
  let l:rel = s:relpath(a:root)
  if empty(l:rel)
    return a:prompt
  endif
  let l:ref = a:line1 == a:line2 ? printf('`%s:%d`', l:rel, a:line1)
        \ : printf('`%s:%d-%d`', l:rel, a:line1, a:line2)
  return empty(a:prompt) ? ('Посмотри ' . l:ref) : (l:ref . ' — ' . a:prompt)
endfunction

function! s:size(vertical) abort
  let l:want = get(g:, 'loca_size', 0)
  if l:want > 0
    return l:want
  endif
  return a:vertical ? float2nr(&columns * 0.42) : float2nr(&lines * 0.4)
endfunction

function! s:open_window() abort
  let l:where = get(g:, 'loca_split', 'botright vertical')
  if l:where ==# 'tab'
    tabnew
    return
  endif
  let l:vertical = l:where =~# 'vert'
  execute l:where . ' new'
  if l:vertical
    execute 'vertical resize ' . s:size(1)
  else
    execute 'resize ' . s:size(0)
  endif
endfunction

function! s:on_exit(...) abort
  " перечитать буферы: агент мог поправить файлы
  silent! checktime
endfunction

function! s:announce(root) abort
  let l:short = fnamemodify(a:root, ':~')
  if s:clean(a:root) ==# s:clean(expand('~'))
    echohl WarningMsg
    echomsg '[loca] корень — домашний каталог. Откройте файл проекта или задайте g:loca_root'
    echohl None
  else
    echomsg '[loca] ' . l:short
  endif
endfunction

function! s:term(argv, root) abort
  if !s:has_term
    call s:err('нужен Vim 8 с +terminal или Neovim. Запустите loca в терминале сам.')
    return
  endif
  call s:announce(a:root)
  call s:open_window()
  if has('nvim')
    call termopen(a:argv, {'cwd': a:root, 'on_exit': function('s:on_exit')})
    setlocal nonumber norelativenumber signcolumn=no
    startinsert
  else
    let l:opts = {'curwin': 1, 'cwd': a:root, 'exit_cb': function('s:on_exit'),
          \ 'term_finish': 'open', 'term_kill': 'term'}
    if term_start(a:argv, l:opts) == 0
      call s:err('не удалось запустить ' . string(a:argv))
    endif
  endif
endfunction

" :Loca / :Loca! — интерактивный агент в терминале
function! s:run(bang, count, line1, line2, prompt) abort
  let l:root = s:root()
  let l:prompt = s:with_ref(a:prompt, l:root, a:count, a:line1, a:line2)
  let l:extra = a:bang ? ['--yolo'] : []
  if !a:bang && !empty(l:prompt)
    let l:extra += ['--mode', 'accept-edits']
  endif
  call s:term(s:argv(l:extra, l:prompt), l:root)
endfunction

" :LocaAsk — ответ без правок, в scratch-буфер
function! s:ask(count, line1, line2, prompt) abort
  let l:root = s:root()
  let l:prompt = s:with_ref(a:prompt, l:root, a:count, a:line1, a:line2)
  if empty(l:prompt)
    call s:err('нужен запрос: :LocaAsk объясни этот модуль')
    return
  endif
  " в plan-режиме агент иначе может начать составлять план вместо ответа
  let l:argv = s:argv(['-p', '--mode', 'plan'],
        \ 'Ответь на вопрос, ничего не меняя и не составляя план. ' . l:prompt)
  echo '[loca] думаю…'
  let l:cwd = getcwd()
  try
    execute 'lcd ' . fnameescape(l:root)
    let l:out = has('nvim') ? system(l:argv) : system(join(map(copy(l:argv), 'shellescape(v:val)')))
  finally
    execute 'lcd ' . fnameescape(l:cwd)
  endtry
  redraw
  if v:shell_error && empty(l:out)
    call s:err('loca вернул код ' . v:shell_error)
    return
  endif
  call s:open_window()
  enew
  setlocal buftype=nofile bufhidden=hide noswapfile nobuflisted filetype=markdown wrap
  silent file [loca]
  let l:lines = split(substitute(l:out, '\e\[[0-9;]*m', '', 'g'), '\n', 1)
  while len(l:lines) > 1 && empty(l:lines[-1])
    call remove(l:lines, -1)
  endwhile
  while len(l:lines) > 1 && empty(l:lines[0])
    call remove(l:lines, 0)
  endwhile
  call setline(1, l:lines)
  setlocal nomodifiable
  normal! gg
endfunction

command! -bang -nargs=* -range -complete=file Loca
      \ call s:run(<bang>0, <range>, <line1>, <line2>, <q-args>)
command! -nargs=+ -range LocaAsk
      \ call s:ask(<range>, <line1>, <line2>, <q-args>)
command! -nargs=* LocaTest
      \ call s:run(0, 0, 0, 0, 'прогони тесты ' . <q-args> . ' и исправь падения')
command! LocaResume call s:term(s:argv(['-c'], ''), s:root())
command! LocaRoot echo '[loca] корень проекта: ' . s:root()

" <unique> + silent! — не перетираем уже занятые привязки (coc, свои и т. п.)
if !get(g:, 'loca_no_mappings', 0)
  silent! nnoremap <unique> <silent> <leader>ll :Loca<CR>
  silent! nnoremap <unique> <leader>lp :Loca<Space>
  silent! xnoremap <unique> <leader>ll :Loca<Space>
  silent! nnoremap <unique> <leader>la :LocaAsk<Space>
  silent! xnoremap <unique> <leader>la :LocaAsk<Space>
  silent! nnoremap <unique> <silent> <leader>lt :LocaTest<CR>
  silent! nnoremap <unique> <silent> <leader>lr :LocaResume<CR>
endif

" агент правит файлы на диске — без autoread буферы разъедутся
if !&autoread
  set autoread
endif
