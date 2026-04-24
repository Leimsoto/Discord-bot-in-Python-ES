# Guía de Integración Frontend - TortuguBot Panel Web

Esta guía detalla la arquitectura, los endpoints y los flujos de autenticación del backend FastAPI, proporcionando toda la información necesaria para construir el frontend (React, Next.js, Vue, etc.) del panel de control de TortuguBot.

## Arquitectura del Backend

El backend se ha migrado a una robusta aplicación **FastAPI** ubicada en la carpeta `api/`.
- **Inyección de Dependencias:** El backend comparte la instancia `DatabaseManager` del bot de Discord, asegurando consistencia en los datos y ahorrando conexiones a la base de datos (se accede mediante `app.state.db`).
- **CORS:** Ya está configurado para aceptar peticiones desde `http://localhost:3000` y `http://localhost:5173` (Vite). Para producción, asegúrate de definir la variable `DASHBOARD_URL` en tu archivo `.env`.
- **Documentación Interactiva (Swagger):** FastAPI genera automáticamente documentación de la API en `/api/docs` (Swagger UI) y `/api/redoc` (ReDoc). Úsalas para explorar y probar endpoints directamente desde el navegador.

---

## Variables de Entorno Relevantes para el Frontend

| Variable | Descripción | Default |
|----------|-------------|---------|
| `DASHBOARD_URL` | URL del frontend (CORS y redirect post-login) | `http://localhost:3000` |
| `API_BASE_URL` | URL del backend API (redirect_uri para OAuth2) | `http://localhost:8080` |
| `DISCORD_CLIENT_ID` | ID de la aplicación de Discord | — |
| `DISCORD_CLIENT_SECRET` | Secret de la aplicación de Discord | — |
| `JWT_SECRET` | Clave para firmar tokens JWT | — |

> [!IMPORTANT]
> `API_BASE_URL` y `DASHBOARD_URL` son URLs **diferentes**. La primera es donde corre el backend FastAPI y la segunda donde corre tu app de frontend. Discord redirige al backend (API_BASE_URL), no al frontend.

---

## Flujo de Autenticación (Discord OAuth2)

El backend maneja la autenticación mediante Discord OAuth2 y entrega un token JWT al frontend para manejar las sesiones de manera segura.

```
┌──────────┐     1. GET /api/auth/login     ┌─────────┐
│ Frontend │ ─────────────────────────────▶ │ Backend │
│  (SPA)   │                                │ FastAPI │
└────┬─────┘                                └────┬────┘
     │                                           │
     │  2. Redirect a Discord OAuth2             │
     │◄──────────────────────────────────────────│
     │                                           │
     │  3. Usuario autoriza en Discord           │
     │                                           │
     │  4. Discord redirige a                    │
     │     API_BASE_URL/api/auth/callback?code=  │
     │                             ─────────────▶│
     │                                           │
     │  5. Backend intercambia code por token,   │
     │     genera JWT, redirige al frontend con  │
     │     ?token=xxx                            │
     │◄──────────────────────────────────────────│
```

### Pasos detallados

1. **Iniciar Sesión:**
   El frontend debe redirigir al usuario al endpoint de login del backend:
   `GET /api/auth/login`
   *Esto redirigirá al usuario a la página de autorización de Discord.*

2. **Callback y Token:**
   Tras autorizar, Discord redirige al usuario al callback del **backend** (no del frontend):
   `GET API_BASE_URL/api/auth/callback?code=...`
   El backend intercambia el código por un access_token de Discord, genera un token JWT propio y redirige de vuelta al frontend (`DASHBOARD_URL`) incluyendo el token como query param.
   
3. **Peticiones Autenticadas:**
   Para todas las peticiones a rutas protegidas (`/api/guild/...` o `/api/auth/me`), el frontend debe enviar el JWT en las cabeceras HTTP:
   ```http
   Authorization: Bearer <tu_jwt_token>
   ```

4. **Obtener el Usuario Actual:**
   `GET /api/auth/me`
   Retorna los datos del usuario logueado y los servidores (guilds) en los que tiene permisos de administración.
   
   **Respuesta:**
   ```json
   {
     "user_id": "123456789012345678",
     "username": "miusuario",
     "guilds": [
       {"id": "987654321", "name": "Mi Servidor", "icon": "abc123"}
     ],
     "is_dev_mode": false
   }
   ```

> [!IMPORTANT]
> Los endpoints que requieren el parámetro `{guild_id}` validan automáticamente que el usuario autenticado (mediante su JWT) tenga los permisos necesarios (Admin/Owner) en ese servidor de Discord. No necesitas validar permisos extras desde el frontend.

> [!WARNING]
> El JWT expira a las **24 horas**. Actualmente no hay mecanismo de refresh token. Si recibes un `401`, redirige al usuario a `/api/auth/login` para re-autenticarse.

---

## Manejo de Errores

Todos los errores de la API siguen el formato estándar de FastAPI:

```json
{
  "detail": "Mensaje de error descriptivo"
}
```

### Códigos HTTP

| Código | Significado | Acción del Frontend |
|--------|-------------|---------------------|
| `200` | Éxito | Procesar respuesta |
| `400` | Request inválido (campos faltantes, JSON inválido) | Mostrar error al usuario |
| `401` | JWT inválido o expirado | Redirigir a `/api/auth/login` |
| `403` | Sin permisos en el guild | Mostrar "Sin acceso" |
| `404` | Recurso no encontrado | Mostrar "No encontrado" |
| `500` | Error interno del servidor | Mostrar error genérico + reintentar |

---

## Ejemplo de Cliente HTTP (Axios)

```typescript
import axios from 'axios';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8080';

const api = axios.create({
  baseURL: API_BASE,
  timeout: 10000,
});

// Inyectar JWT automáticamente
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('jwt_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Interceptor de errores
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('jwt_token');
      window.location.href = `${API_BASE}/api/auth/login`;
    }
    return Promise.reject(error);
  }
);

export default api;

// Uso:
// const { data } = await api.get(`/api/guild/${guildId}/config`);
// await api.put(`/api/guild/${guildId}/config`, { prefix: "!" });
```

---

## Rutas de la API (Endpoints REST)

El backend expone módulos separados por funcionalidad. 

### 1. Configuración General (Guild)
- `GET /api/guild/{guild_id}/config` — Retorna la configuración global y del servidor (idioma, prefijos, logs, configuración de welcome, boost y suggestions incluida).
- `PUT /api/guild/{guild_id}/config` — Actualiza configuraciones generales del servidor.
- `GET /api/guild/{guild_id}/stats` — Retorna estadísticas de uso del bot en el servidor.

### 2. Moderación
- `GET /api/guild/{guild_id}/moderation/actions` — Historial general de moderación del servidor (paginado: `?limit=50&offset=0`).
- `GET /api/guild/{guild_id}/moderation/user/{uid}` — Historial detallado de infracciones de un usuario específico.
- `GET /api/guild/{guild_id}/moderation/warns` — Lista de usuarios con advertencias activas actualmente.

### 3. Tickets
- `GET /api/guild/{guild_id}/tickets/config` — Obtiene la configuración de los canales de tickets y sus categorías creadas.
- `PUT /api/guild/{guild_id}/tickets/config` — Actualiza opciones de tickets (máx tickets, cooldown, canales de log).
- `GET /api/guild/{guild_id}/tickets` — Lista completa (paginada: `?limit=50&offset=0&status=OPEN`) de todos los tickets abiertos y cerrados.
- `GET /api/guild/{guild_id}/tickets/{id}` — Detalle específico de un ticket (incluyendo transcripciones).

### 4. Tags (Respuestas Rápidas)
- `GET /api/guild/{guild_id}/tags` — Lista todos los tags (comandos de respuesta de texto) del servidor.
- `POST /api/guild/{guild_id}/tags` — Crea un nuevo tag. Body: `{"name": "faq", "content": "..."}`.
- `PUT /api/guild/{guild_id}/tags/{name}` — Edita el contenido de un tag. Body: `{"content": "nuevo contenido"}`.
- `DELETE /api/guild/{guild_id}/tags/{name}` — Elimina un tag.

### 5. Custom Commands (Comandos Programables)
- `GET /api/guild/{guild_id}/custom-commands` — Lista todos los custom commands del servidor.
- `POST /api/guild/{guild_id}/custom-commands` — Crea un nuevo custom command.
- `GET /api/guild/{guild_id}/custom-commands/{name}` — Detalle de un custom command.
- `PUT /api/guild/{guild_id}/custom-commands/{name}` — Edita un custom command.
- `DELETE /api/guild/{guild_id}/custom-commands/{name}` — Elimina un custom command.
- `GET /api/guild/{guild_id}/custom-commands/variables` — Lista variables persistentes del servidor.
- `PUT /api/guild/{guild_id}/custom-commands/variables/{key}` — Actualiza una variable.

### 6. Niveles y XP (Levels)
- `GET /api/guild/{guild_id}/levels/config` — Obtiene la configuración del sistema de experiencia y niveles.
- `PUT /api/guild/{guild_id}/levels/config` — Modifica multiplicadores y opciones generales de XP.
- `GET /api/guild/{guild_id}/levels/leaderboard` — Obtiene la tabla de posiciones (top usuarios).
- `GET /api/guild/{guild_id}/levels/rewards` — Lista de recompensas/roles automáticos por nivel.
- `GET /api/guild/{guild_id}/levels/user/{uid}` — Obtiene progreso específico y ranking actual de un usuario.

### 7. Reportes
- `GET /api/guild/{guild_id}/reports` — Lista de reportes realizados por los usuarios (`?status=PENDING`).
- `GET /api/guild/{guild_id}/reports/{id}` — Detalle de un reporte.
- `PUT /api/guild/{guild_id}/reports/{id}` — Actualiza estado. Body: `{"status": "RESOLVED"}`.

### 8. Tareas Programadas (Schedules)
- `GET /api/guild/{guild_id}/schedules` — Lista las tareas y mensajes automatizados del servidor.
- `POST /api/guild/{guild_id}/schedules` — Crea una nueva tarea (cronjob).
- `PUT /api/guild/{guild_id}/schedules/{id}` — Modifica opciones de una tarea activa.
- `DELETE /api/guild/{guild_id}/schedules/{name}` — Elimina una tarea programada.

### 9. Sorteos (Giveaways)
- `GET /api/guild/{guild_id}/giveaways` — Historial de todos los sorteos o filtrado para los activos.
- `GET /api/guild/{guild_id}/giveaways/{msg_id}` — Detalles en vivo (participantes, premio, condiciones) de un sorteo.

### 10. Autoroles y Reaction Roles (Autoroles)
- `GET /api/guild/{guild_id}/autoroles` — Lista de asignaciones de autorol/reacción configurados.
- `POST /api/guild/{guild_id}/autoroles` — Crea o actualiza un panel de autorol asociado a un mensaje (requiere `message_id`, `channel_id`, y un JSON `mapping_data`).
- `DELETE /api/guild/{guild_id}/autoroles/{message_id}` — Elimina un autorol.

### 11. Radio 24/7 (Radio)
- `GET /api/guild/{guild_id}/radio/config` — Obtiene la configuración de la radio (estado `enabled`, `channel_id`, `stream_url`, `volume`).
- `PUT /api/guild/{guild_id}/radio/config` — Enciende/Apaga la radio o cambia la estación de audio.

### 12. Embeds Personalizados (Embeds)
- `GET /api/guild/{guild_id}/embeds` — Obtiene todas las plantillas de embeds guardadas en el servidor.
- `GET /api/guild/{guild_id}/embeds/{name}` — Detalle (el JSON estructurado) de una plantilla específica.
- `POST /api/guild/{guild_id}/embeds` — Guarda una nueva plantilla de embed. Body: `{"name": "...", "embed_data": "{...}"}`.
- `DELETE /api/guild/{guild_id}/embeds/{id}` — Borra una plantilla.

### 13. Configuración Fina de Canales (Channels)
- `GET /api/guild/{guild_id}/channels` — Lista las configuraciones individuales de todos los canales con ajustes especiales.
- `PUT /api/guild/{guild_id}/channels/{channel_id}` — Actualiza permisos específicos de un canal (ej. `ignore_xp`, `ignore_commands`, etc).

---

## 💡 Recomendaciones de Implementación (Frontend)

1. **Gestor de Estado (Context/Zustand/Redux):** 
   Crea un estado global para mantener de forma accesible el `JWT` y el objeto del `user` (devuelto por `/api/auth/me`). Esto te permitirá renderizar condicionalmente componentes si el usuario está autenticado.

2. **Cliente HTTP Global (Interceptors):** 
   Configura una instancia global de Axios o Fetch que inyecte automáticamente la cabecera `Authorization: Bearer <token>` en todas las peticiones a `/api/...`. Configura un interceptor de errores para que, si el backend devuelve `401 Unauthorized`, cierre sesión automáticamente y redirija al login. Consulta el ejemplo de Axios más arriba.

3. **Selector de Servidores (Guilds):** 
   Al cargar `/api/auth/me`, recibirás una lista de servidores (`guilds`) gestionables por el usuario. Crea un menú desplegable global o barra lateral (Sidebar) para seleccionar un servidor activo. Guarda el `guild_id` seleccionado en estado y úsalo de base para inyectarlo en todas las URLs protegidas (`/api/guild/${activeGuildId}/...`).

4. **Paginación:**
   Los endpoints que retornan listas largas (tickets, acciones de moderación) soportan paginación mediante `?limit=N&offset=M`. Implementa scroll infinito o paginación numérica en el frontend.

---

## 🔮 Roadmap Futuro

- **WebSockets:** Para datos en tiempo real (stats del bot, tickets nuevos, eventos de moderación), se planea exponer un endpoint WebSocket (`/api/ws`) en una versión futura. Esto eliminará la necesidad de polling.
- **Acciones en Vivo:** Actualmente los endpoints son solo CRUD sobre la DB. En el futuro se expondrán endpoints que ejecuten acciones en Discord directamente (enviar mensajes, kick/ban desde el panel).
- **Tipos TypeScript:** Se planea generar interfaces TypeScript automáticas desde los Pydantic models del backend para type-safety en el frontend.
