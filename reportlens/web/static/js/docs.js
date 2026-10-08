// Swagger UI for /docs (an external file: the Content-Security-Policy allows no inline scripts).
/* global SwaggerUIBundle */
window.addEventListener("DOMContentLoaded", () => {
  SwaggerUIBundle({
    url: "/api/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    docExpansion: "list",
    defaultModelsExpandDepth: 0,
    tryItOutEnabled: false,
    withCredentials: true,
  });
});
