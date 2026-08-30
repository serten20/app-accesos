# CLAUDE.md — Contexto del proyecto ACCESOS

Guía para trabajar en este repositorio. Recoge las **decisiones de diseño no obvias**
y las **trampas conocidas**, que es lo que no se deduce leyendo el código a secas.
Para la descripción funcional de cara al usuario, ver [README.md](README.md).

---

## 1. Qué es

**ACCESOS — Security Access Manager**: aplicación web interna para controlar la
**rotación periódica de contraseñas** de acceso a clientes/empresas por parte de un
equipo de técnicos. Avisa por email, escala a administradores si nadie confirma, y
ofrece dashboard, reportes, auditoría y papelera.

- **Stack:** FastAPI + Jinja2 + SQLAlchemy + SQLite · APScheduler · smtplib/paramiko ·
  passlib(bcrypt) · itsdangerous · cryptography(Fernet) · psutil.
- **Front:** server-rendered (Jinja2) + Tailwind/DaisyUI y Chart.js **por CDN**.
  No hay build step, ni npm, ni React. El diseño (tema oscuro "cyber/SOC",
  glassmorphism, acento azul→índigo) está escrito a mano en bloques `<style>`
  dentro de las plantillas.
- **Despliegue:** Docker Compose. `uvicorn ... --workers 2` (ojo, ver §6).
- **Idioma:** toda la interfaz y los emails están **en español**.

---

## 2. Arranque

```bash
cp .env.example .env          # editar SECRET_KEY y ADMIN_PASSWORD como mínimo
docker compose up -d --build  # http://localhost:8000
```

Despliegue incremental en producción: `git pull && docker compose up -d --build`.
El esquema se inicializa/actualiza solo (Alembic con fallback a `create_all` +
migraciones manuales idempotentes en `database.py`): **no hay que migrar a mano**.

---

## 3. Mapa del código

| Fichero | Responsabilidad |
|---|---|
| `main.py` | App FastAPI, lifespan, login/logout, `/forgot`, `/reset`, `/healthz` |
| `models.py` | Modelos + **lógica de dominio en properties** + `roll_company_cycle()` |
| `thresholds.py` | Umbrales configurables del semáforo (con caché en memoria) |
| `database.py` | Engine, init/migraciones, `get_setting`/`set_setting` |
| `auth.py` | Sesiones, hashing, decoradores de rol, tokens de confirmación y reseteo |
| `crypto.py` | `SECRET_KEY` + cifrado Fernet de secretos en reposo |
| `security.py` | Middleware CSRF, anti fuerza bruta |
| `scheduler.py` | Jobs horarios + **todo el envío de email y sus plantillas** |
| `compliance.py` | Cálculo de % de cumplimiento y snapshots diarios |
| `db_admin.py`, `remote_backup.py`, `trash.py` | BD/backups, copia SFTP-FTP, papelera |
| `routers/` | `admin` (grande), `technician`, `reports`, `confirm`, `profile` |
| `tmpl.py` | Instancia única de Jinja2 + filtros `fmt_date` / `fmt_datetime` |

---

## 4. Modelo de dominio (lo importante)

### 4.1 Dos relojes distintos

Esta es **la fuente de casi todos los bugs históricos**. Cada empresa tiene:

1. **Reloj por técnico** — `TechnicianCompany.last_changed`: cada técnico tiene el
   suyo para cada empresa. Al confirmar una rotación **solo se reinicia el suyo**.
   Es lo que ven el portal del técnico y la tabla "Estado de confirmación".
2. **Reloj global de la empresa** — `Company.last_changed` (la "Fecha de inicio"):
   ancla de los **ciclos** de la empresa.

> ⚠️ Al tocar cualquier vista, decide **explícitamente** cuál de los dos aplica.
> Mezclarlos produjo el bug de "estado OK pero días EXPIRADO".

### 4.2 Ciclos de la empresa (`roll_company_cycle`)

`Company.last_changed` avanza en **pasos completos de `expiry_days`** alineados a la
fecha de inicio (p. ej. 16/04 → 15/06 → 14/08). Un ciclo **rueda** solo si:

- **TODOS** los técnicos asignados lo confirmaron (se comprueba contra
  `RotationHistory`: cada técnico necesita una rotación con `rotated_on >= inicio_ciclo`), o
- la empresa **no tiene técnicos** → rueda siempre (nunca expira).

Si falta alguien, la empresa queda **expirada** hasta que todos confirmen (una
confirmación tardía la recupera). El rodaje se dispara al confirmar, al abrir la
lista/ficha de empresas, y en el job horario — así se **autocorrige** sin reconfirmar.

### 4.3 Semáforo y umbrales

`crítico ≤ N` / `aviso ≤ M` / resto OK, con **N y M configurables** en
*Configuración → General* (por defecto **2** y **7**). Toda la clasificación pasa por
`thresholds.classify()`, así que cambiar el umbral se propaga solo a paneles,
portales, gráficos, compliance y emails.

- `thresholds.get_thresholds()` **cachea en memoria** (se llama muchísimo);
  hay que llamar a `reset_cache()` al guardar la configuración.
- Si añades una vista que muestre umbrales **en texto**, pásale `warn_days`/`crit_days`
  desde la ruta. Olvidarlo provocó un 500 en `/admin/my-companies`.

### 4.4 Cumplimiento

Solo penaliza el estado **CRÍTICO**; ATENCIÓN no penaliza. Unidades = empresas +
técnicos con asignaciones. `job_daily_snapshot` guarda 1 fila/día
(`ComplianceSnapshot`) para la gráfica de tendencia (ventana 7/30/60/90 días).

---

## 5. Sistema de email

### 5.1 Tipos (`EmailLog.kind`)

`alert` (técnico) · `report` (admins) · `escalation` (admins) · `welcome` ·
`password_reset` · `db_alert` · `smtp_alert` · `test` · `other`.

### 5.2 Plantillas editables

`alert`, `report`, `welcome` y `escalation` son **editables** desde Configuración
(asunto + cuerpo HTML, con chips de variables y vista previa en vivo). Se guardan en
`AppSettings`; si están vacías se usa el `DEFAULT_*` de `scheduler.py`.

> ⚠️ **Trampa importante:** si cambias las variables de una plantilla, los despliegues
> que **guardaron** la versión anterior seguirán usándola y mostrarán `{variables}`
> literales. Pasó al agrupar el escalado. Solución aplicada (replicar si vuelve a ocurrir):
> `escalation_effective_template()` detecta la plantilla antigua y usa la nueva,
> **y** el mapping rellena también las variables viejas con agregados.

### 5.3 Compatibilidad con Outlook de escritorio

El motor de render de Outlook es **Word**. Todas las plantillas de email deben ser
"bulletproof":

- Maquetación con **tablas** y `bgcolor` **en cada celda** (nada de fondos en `div`).
- **Ghost tables MSO** (`<!--[if mso]>`) para fijar el ancho.
- Botones CTA con **VML** (`v:roundrect`), vía `_cta_button()`.
- **Prohibido:** `linear-gradient`, `border-radius`, `display:grid/flex`.
- Tema **claro**. `_wrap_html_email()` añade doctype, namespace VML y `color-scheme`.

### 5.4 Robustez de la cola de email

- Las alertas avanzan `alert_last_sent`/`alert_count` **aunque falle el envío**
  (la entrega la reintenta el job) → evita reencolar duplicados.
- Si SMTP no está configurado, `job_check_alerts` **omite el barrido** entero.
- El reintento **deduplica** alertas/escalados por (destinatario, asunto): al
  recuperarse SMTP se entrega **solo el más reciente**; el resto → `superseded`.
- Agotados 5 intentos → `abandoned`, se avisa a los admins (auditoría **siempre** +
  email con throttle de 12 h) y se purgan los estados terminales a los 30 días.
- El **escalado va agrupado por empresa**: un solo email lista a todos sus técnicos
  pendientes (variables `{company}`, `{n_tecnicos}`, `{tecnicos}`).

---

## 6. Scheduler y el lock de worker

`uvicorn` corre con **`--workers 2`**: cada proceso ejecuta el `lifespan`, así que sin
protección habría **dos schedulers** y cada tarea se ejecutaría dos veces (causó
alertas duplicadas en producción).

`_acquire_scheduler_lock()` toma un `fcntl.flock` exclusivo sobre
`data/scheduler.lock`; **solo el worker que lo obtiene** arranca el scheduler, el resto
recibe `start_scheduler() → None`. En los logs debe verse "Scheduler iniciado" **una
sola vez** y "Scheduler NO iniciado…" en el otro worker.

Jobs (todos horarios): alertas+escalado · reporte periódico · salud de BD+backup ·
snapshot de cumplimiento + rodaje de ciclos · reintento/purga de emails.

> `job_check_alerts` **no** tiene `next_run_time` inmediato a propósito: arrancarlo en
> cada reinicio era otra fuente de duplicados.

---

## 7. Seguridad (estado auditado)

- **Sin credenciales hardcodeadas.** `SECRET_KEY` viene del entorno o se genera y
  persiste en `data/secret_key` (0600). Secretos SMTP/SFTP **cifrados con Fernet**
  (prefijo `enc:`). Contraseñas de usuario en **bcrypt**.
- **Sin SQL injection**: todo pasa por el ORM (los `f-string` solo construyen
  *patrones* para `ilike`, que viajan parametrizados). El único SQL crudo con
  interpolación usa nombres de tabla del propio `inspect(engine)`.
- **La BD no está expuesta**: el único `StaticFiles` montado es `/static`; `data/` no
  se sirve. La descarga de backup exige `@require_admin`.
- **No hay auto-registro**: las únicas rutas de creación exigen `@require_admin`.
- CSRF (double-submit), sesiones con invalidación en servidor (`tokens_valid_from`),
  anti fuerza bruta en login, y reseteo de contraseña por enlace firmado de **un solo
  uso** (huella del hash actual) con rate-limit y sin enumeración de cuentas.
- Pendiente operativo: definir `ADMIN_PASSWORD` en `.env` y poner `COOKIE_SECURE=true`
  si algún día se expone por HTTPS.

---

## 8. Cómo validar cambios (patrón obligatorio)

**No hay suite de tests.** La validación se hace ejecutando código real en Docker
contra una **copia** de la BD, nunca contra `data/accesos.db`:

```bash
export MSYS_NO_PATHCONV=1     # Git Bash en Windows
proj="/ruta/al/repo"
cp "$proj/data/accesos.db" "$proj/data/zsmoke_x.db"
docker run --rm -v "${proj}:/app" -w //app \
  -e DATABASE_URL="sqlite:///./data/zsmoke_x.db" app-accesos-web python _test_x.py
rm -f "$proj/data/zsmoke_x.db" "$proj/_test_x.py"
```

Convenciones: comprobar siempre `import` primero; ficheros temporales con prefijo
`_` y BDs de prueba `zsmoke_*.db`; **borrarlos al terminar**. Para probar rutas
protegidas: `TestClient` + `create_session_token(user.id)` en la cookie `session_token`,
y hacer un GET previo para obtener el `csrf_token` antes de cualquier POST.

Para aislar el envío de correo, sustituir `scheduler._send_email` por un capturador.

---

## 9. Convenciones y trampas conocidas

- **Fechas y horas:** todo se guarda en **UTC**. En plantillas, usar los filtros
  `| fmt_datetime` / `| fmt_date` (convierten a la zona configurada). Un `.strftime()`
  directo muestra UTC → ese fue el bug de las horas del registro de emails.
  En los emails, las fechas van en `DD/MM/AAAA`.
- **`TechnicianCompany.last_changed` tiene `default=date.today`**: asignarle `None`
  al insertar **no** deja el campo vacío, dispara el default. Para heredar la fecha de
  la empresa hay que copiarla explícitamente.
- **Windows/Git Bash:** `docker run` necesita `MSYS_NO_PATHCONV=1` y `-w //app`.
  Para mensajes de commit largos, usar `git commit -F fichero` (los here-strings y
  caracteres como `>` dan problemas).
- **No versionar** `data/`, `uploads/`, `logs/`, `.env` (ya en `.gitignore`).
- **`gh` no está instalado** en el entorno de desarrollo: los PRs se abren a mano
  desde `https://github.com/serten20/app-accesos/pull/new/<rama>`.
- Rama de trabajo actual: **`feature/print-buttons-reports`** (rama principal: `main`).
