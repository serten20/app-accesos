# 🔐 ACCESOS — Security Access Manager

Aplicación web para **gestionar la rotación de contraseñas de acceso** a clientes/empresas por parte de un equipo de técnicos: controla cuándo toca rotar cada acceso, avisa por email, escala a los administradores si no se confirma, y ofrece dashboard, reportes y auditoría completa.

Construida con **FastAPI + SQLite**, pensada para desplegarse en **Docker** en minutos.

---

## ✨ Funcionalidades

### Gestión de accesos
- **Empresas/clientes** con días de expiración, VPN, documentación y **fecha de inicio** configurable.
- **Técnicos** asignados a empresas (cada técnico con **su propio contador de rotación**: la ficha del técnico refleja su estado individual, no el global de la empresa).
- **Roles:** administrador, técnico, **auditor** (solo lectura) y **doble rol** (admin que también es técnico). Conversión técnico ↔ admin en ambos sentidos.
- **Semáforo de estado con umbrales configurables** (crítico / atención, por defecto 2 y 7 días) desde *Configuración → General*; el cambio se propaga a todos los paneles y portales al instante.

### Alertas y notificaciones
- **Alertas por email** a los técnicos cuando un acceso está próximo a expirar, con **confirmación en 1 clic** desde el propio correo (magic link).
- **Escalado automático** a administradores (o emails externos) si el técnico no confirma tras N avisos.
- **Email de bienvenida** opcional al crear técnicos (usuario, contraseña temporal y enlace de acceso).
- **Plantillas de email editables** (alerta, reporte, bienvenida) y **compatibles con Outlook de escritorio** (maquetación a prueba de su motor de render).

### Reportes y analítica
- **Reportes periódicos** a administradores + centro de reportes: salud global, cuentas en riesgo, **ranking de puntualidad**, **matriz de acceso** y **calendario + heatmap** de vencimientos.
- **Dashboard** con % de cumplimiento, gráficas de tendencia (Chart.js) y próximos vencimientos.
- **Vistas imprimibles / exportables a PDF** desde el navegador.
- **Historial de rotaciones** con **filtros** (rango de fechas, presets, técnico, empresa), **exportación a CSV/TXT** y enlaces directos a las fichas.

### Datos, importación y migración
- **Importación masiva** por CSV/TXT: empresas, técnicos y **asignaciones técnico↔empresa**.
- **Exportación "para migración"** que devuelve empresas, técnicos y asignaciones en el **mismo formato de importación** (round-trip), ideal para mover o reconstruir la configuración.
- **Registro de emails** con reintentos automáticos, **deduplicación** de avisos acumulados (solo se entrega el más reciente al recuperar SMTP), estado **abandonado** tras agotar intentos y **aviso a administradores** si el correo falla.
- **Papelera** (archivado lógico recuperable) y **auditoría** completa.
- **Base de datos:** monitorización en vivo, VACUUM, **backups** locales (con **descarga** directa) y **externos por SFTP/FTP** (con prueba de conexión), purga configurable.

### Personalización y ayuda
- **Nombre de la empresa configurable** (aparece en el pie del login; vacío por defecto).
- **Zona horaria** configurable.
- **Wizard de bienvenida** y tooltips de ayuda contextuales.

### Seguridad
- Protección **CSRF** (double-submit), cookies de sesión con invalidación en servidor, **anti fuerza bruta** en el login.
- **Secretos cifrados en reposo** (SMTP/SFTP) con Fernet derivado de `SECRET_KEY`.
- Logs rotativos a fichero y endpoint **`/healthz`** para monitores/balanceadores.

---

## 🧱 Stack

FastAPI · Jinja2 · SQLAlchemy + SQLite · Alembic · APScheduler · smtplib/paramiko · passlib (bcrypt) · itsdangerous · cryptography · psutil · DaisyUI/Tailwind + Chart.js (CDN).

---

## 🚀 Puesta en marcha (Docker — recomendado)

Requisitos: **Docker** y **Docker Compose**.

```bash
# 1. Clona el repositorio
git clone https://github.com/TU_USUARIO/TU_REPO.git
cd TU_REPO

# 2. Crea tu fichero de entorno a partir del ejemplo
cp .env.example .env
#   → edita .env y cambia SECRET_KEY y ADMIN_PASSWORD como mínimo

# 3. Arranca
docker compose up -d --build
```

La aplicación queda disponible en **http://localhost:8000**.

En el primer arranque se crea automáticamente una base de datos vacía y un **administrador por defecto**:

| Usuario | Contraseña | Notas |
|---------|------------|-------|
| `admin` (o `ADMIN_USERNAME`) | `admin123` (o `ADMIN_PASSWORD`) | Forzará cambio de contraseña en el primer login |

> ⚠️ **Cambia `ADMIN_PASSWORD` en tu `.env` antes de desplegar.** El valor de ejemplo es público.

---

## ⚙️ Configuración (`.env`)

| Variable | Descripción |
|----------|-------------|
| `SECRET_KEY` | Clave para firmar sesiones y cifrar secretos. **Cámbiala** por una larga y aleatoria. Si se deja vacía, se genera y persiste en `data/secret_key`. |
| `DATABASE_URL` | URL de la BD. Por defecto `sqlite:////app/data/accesos.db`. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` | Servidor de correo para alertas/reportes. *(También configurable desde la propia consola → Configuración → SMTP.)* |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_EMAIL` | Credenciales del admin inicial (solo primer arranque). |

La mayoría de ajustes (SMTP, alertas, reportes, copia externa, URL pública, zona horaria…) se gestionan **desde la interfaz**, en **Configuración**.

> Para que los **enlaces de los emails** funcionen, configura la **URL pública** en *Configuración → General*, **incluyendo el puerto** si no usas un proxy inverso (la app escucha en el 8000) — p. ej. `http://192.168.1.50:8000` o `https://accesos.tuempresa.com`.

---

## 🗂️ Estructura del proyecto

```
.
├── main.py              # App FastAPI, lifespan, login/logout, /healthz
├── models.py            # Modelos SQLAlchemy
├── database.py          # Engine, init/migraciones
├── auth.py              # Sesiones, hashing, decoradores de rol
├── crypto.py            # SECRET_KEY + cifrado de secretos
├── security.py          # Middleware CSRF, anti fuerza bruta
├── scheduler.py         # Jobs (alertas, reportes, salud, emails) + envío SMTP
├── compliance.py        # Cálculo de cumplimiento
├── thresholds.py        # Umbrales de estado configurables (con caché)
├── db_admin.py          # Monitorización y mantenimiento de la BD
├── remote_backup.py     # Copia externa SFTP/FTP
├── trash.py             # Papelera (archivado lógico)
├── routers/             # admin, technician, reports, confirm, profile
├── templates/           # Vistas Jinja2 (DaisyUI/Tailwind)
├── static/              # JS (onboarding…)
├── alembic/             # Migraciones
├── data/                # BD, secret_key, backups, imports  (NO se versiona)
├── logs/  uploads/      # Runtime  (NO se versiona)
├── Dockerfile  docker-compose.yml
└── requirements.txt
```

---

## 🧠 Conceptos clave

- **Último cambio / fecha de inicio:** fecha ancla desde la que se cuenta la expiración de cada empresa. Vacía = hoy; puede ser pasada o futura.
- **Días de expiración:** cada cuántos días debe rotarse la contraseña. *Vencimiento = fecha de inicio + días de expiración.*
- **Semáforo:** `CRÍTICO` / `AVISO` / `OK` según los días restantes. Los umbrales son **configurables** en *Configuración → General* (por defecto `CRÍTICO ≤ 2` y `AVISO ≤ 7` días; el resto es `OK`).
- **Cumplimiento (%):** solo penalizan los elementos **críticos**. Unidades = empresas + técnicos con asignaciones.
- **Rotación:** cuando un técnico confirma el cambio (en la app o por el enlace del email), **su** contador se reinicia y dejan de enviarse avisos (estado por técnico, independiente del resto).

---

## 🔄 Tareas programadas

El scheduler (APScheduler) ejecuta cada hora:

- **Comprobación de alertas + escalado** (omitida si SMTP no está configurado; el estado del aviso avanza aunque el envío falle, para no re-encolar duplicados).
- **Envío de reportes** según el horario configurado.
- **Salud de la BD + backup automático.**
- **Snapshot de cumplimiento** (1 al día) para la gráfica de tendencia.
- **Reintento de emails fallidos** con deduplicación de avisos, marcado como *abandonado* al agotar intentos, **aviso a administradores** ante problemas de envío y purga de registros antiguos.

---

## 🛠️ Desarrollo

```bash
# Logs de la app
docker compose logs -f web

# Acceder a un shell del contenedor
docker exec -it $(docker compose ps -q web) bash
```

El esquema se inicializa solo (Alembic con fallback a `create_all` + migraciones manuales idempotentes), por lo que **no hay que ejecutar migraciones a mano** en un despliegue nuevo.

---

## 💾 Datos y copias de seguridad

- Todo el estado vive en `data/accesos.db` (volumen Docker), junto a `data/secret_key`.
- Backups locales en `data/backups/` (manuales o automáticos diarios) con rotación FIFO.
- Opción de **copia externa** a SFTP/FTP desde *Configuración → Base de datos*.

> Las carpetas `data/`, `logs/` y `uploads/` y el fichero `.env` están **excluidas del control de versiones** (`.gitignore`): nunca se suben secretos ni datos.

---

## 📄 Licencia

Uso interno. Ajusta esta sección según corresponda.
