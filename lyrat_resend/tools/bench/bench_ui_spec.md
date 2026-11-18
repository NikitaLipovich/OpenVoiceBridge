# bench_ui_spec.json — схема пульта стенда (гоал 104)

Один файл описывает, что показывает и что умеет пульт стенда: веб-страница benchd (`benchd_admin.html`) и экран
«Стенд» в приложении `sco_probe` на телефоне. Оба генерирует `gen_bench_ui.py`; руками правится только спецификация
(и рукописные рантаймы: `bench_ui_web.tmpl.html`, `BenchActivity.java`, `BenchTemplate.java`, `MiniJson.java`).

## Верхний уровень

| ключ | что |
|---|---|
| `spec_version` | число; растёт при несовместимой смене схемы |
| `title` | заголовок страницы и экрана |
| `owner` | имя замка владельца (одно для ПК и телефона: замок общий) |
| `poll_ms` | `status`, `job` — период опроса `/status` и задания |
| `texts` | общие тексты рантаймов: `no_service`, `updated`, `lock_other`, `job_started`, `job_state`, `job_running`, `job_ended`, `done`, `refused`, `unknown_route`, `busy` |
| `header` | шаблон строки службы |
| `panels` | панели: `id`, `title`, `fields`, `actions` (группа кнопок этой панели), `results: true` — здесь строка ответа и вывод задания |
| `actions` | действия (кнопки) |
| `feeds` | ленты: события и хвост лога |

## Шаблон

Текст с подстановками `{путь}` или `{путь|формат}`. Путь — ключи через точку в ответе `/status` (`s3.rssi`,
`lock.holder.owner`), число — индекс списка. `$имя` — переменные рантайма: `$device` («страница»/«телефон»),
`$owner`, `$param.<имя>` (значения полей действия), в ответах — `$code`, `$json`; `$time`, `$state`, `$route` в `texts`.
Нет значения или `null` → `—`. Числа: целые без дробной части, иначе до 3 знаков без хвостовых нулей.

Форматы: `int` (округление floor(x+0.5)), `mb1`/`mb0` (байты → МБ с 1/0 знаками), `hhmm`/`hms` (из ISO-времени),
`opt_comma` (`, значение` или пусто), `owners` (список объектов → их `owner` через запятую), `json` (компактный JSON).
Числа-строки (`"-55"` у S3) считаются числами в форматах и сравнениях. Правила одинаковы в Python (эталон в
`gen_bench_ui.py`), JS страницы и Java телефона — это проверяет `tests/test_bench_ui.py`.

## Условие

`{"path": "...", "op": "...", "value": ...}`; `op`: `eq ne lt le gt ge truthy falsy empty nonempty`. Список условий —
«и». Ложны: `null`, `false`, `0`, `""`, пустые список и объект.

## Поле панели

`{"id", "label", "cases": [{"if": [условия], "text": шаблон, "color": "ok|warn|bad|"}], "list": "jobs"?}` — первый
случай, чьи условия выполнены (без `if` — всегда). Пороги цвета — случаями с `lt/le/gt/ge`. С `list` — строка на элемент
списка, пути в `label`/`cases` — относительно элемента.

## Действие

| ключ | что |
|---|---|
| `id`, `label`, `group`, `row` | имя, надпись, группа панели, строка кнопок в группе |
| `method`, `endpoint` | маршрут benchd (`POST`) |
| `body` | постоянные поля запроса (строки — шаблоны); `owner` рантайм добавляет сам |
| `params` | поля ввода: `name`, `label`, `type` (`text`/`enum`/`int`), `choices` (`[[значение, подпись], …]`), `default`, `placeholder` |
| `confirm`, `confirm2` | вопросы перед действием (шаблоны по `/status` и `$param`) |
| `danger` | красная кнопка |
| `requires_lock` | при чужом замке — ещё вопрос `texts.lock_other` и `"force": true` |
| `lan_control` | доступно из сети (телефон, страница не с 127.0.0.1); только для маршрутов benchd с `lan: true` |
| `visible_if` | условия видимости (например, «Режим BT» — только при `bt_mode`) |
| `available_if`, `unavailable_text` | иначе вместо запроса — этот текст |
| `result` | `ok`/`fail` — случаи по JSON ответа; без него: 202 → задание, 200 → `texts.done`, иначе `texts.refused` |

## Лента

`{"id", "kind": "events"|"tail", "title", "method": "GET", "endpoint", "poll_ms", "params", "fixed"}`;
events: `columns`, `hide_keys`, `bad` (regex типа+action для красного); tail: `cursor` (`after`), `limit_param`,
`first_limit`, `limit`, `keep` (строк на странице; на телефоне не больше 300).

## Проверка `gen_bench_ui.py --check`

(a) сгенерированные файлы = то, что даёт спецификация; (b) маршрут, параметры, варианты, имя `/action` есть в
`ROUTES`/`ACTIONS` benchd.py, `lan_control` только для `lan`-маршрутов; (c) пути статуса есть в `/status` benchd
против эмулятора; (d) управляющий маршрут или действие benchd без кнопки — предупреждение (`--strict` — ошибка; маршрут
без кнопки по делу помечается в benchd `ui_exempt`).
