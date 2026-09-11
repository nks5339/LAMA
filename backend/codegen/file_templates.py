"""Per-file-type instructions injected into codegen prompts.

iter-13.20 — extended to cover dotnet / php / ruby / rust so the target
stack selection is honoured end-to-end. Previously only nodejs / python /
java / go were templated and any other backend_lang fell back to a generic
"Generate appropriate content" string, which silently demoted code quality
for .NET, PHP, Ruby, Rust projects.

iter-13.82 — each (file_type, backend_lang) pair now also ships a
CONCRETE ANNOTATED SKELETON the LLM must follow (few-shot beats abstract
rules by a wide margin), and exports REQUIRED_TOKENS used by the post-LLM
structural validator in ``routes/codegen.py`` to reject files missing the
mandatory framework markers (@RestController, @Service, APIRouter,
[ApiController], ...) before they ever reach the user's filesystem.
"""

import textwrap as _tw

FILE_TYPE_INSTRUCTIONS = {
    "bootstrap": {
        "nodejs": "Generate the Express server bootstrap (src/server.js). Boot middleware (helmet, cors, json, logging, request-id, error handler), mount every route module, expose /health + /ready, start on PORT env. NO inline route handlers - only wiring.",
        "python": "Generate FastAPI bootstrap (app/main.py). Lifespan startup/shutdown, include every router, /health + /ready endpoints, CORS, structured logging, Prometheus metrics. NO inline endpoint logic - only wiring.",
        "java":   "Generate the @SpringBootApplication main class. Wire @ComponentScan, Actuator health, OpenAPI bean. NO endpoint logic - only bootstrap.",
        "go":     "Generate cmd/main.go: load config, init DB, build router, mount all handlers, start http.Server with graceful shutdown.",
        "dotnet": "Generate Program.cs (minimal hosting model): WebApplicationBuilder, register all services, MapControllers, UseAuthentication/Authorization, MapHealthChecks, Swagger.",
        "php":    "Generate Laravel bootstrap/app.php with middleware groups + service providers; routes/api.php wires every controller.",
        "ruby":   "Generate config/application.rb + config/routes.rb with `resources :...` for every controller.",
        "rust":   "Generate src/main.rs: tokio runtime, load config, build Axum Router with every handler + middleware layers, run with graceful shutdown.",
    },
    # ── iter-13.115 — per-resource production-grade split ────────────────────
    "mapper": {
        "java":   "Generate a @Component <Resource>Mapper class. Map entity ↔ DTO using VERBATIM OLTP column names. Methods: toDto(<Resource> entity), toEntity(<Resource>Dtos.CreateRequest req), updateEntity(<Resource> entity, <Resource>Dtos.UpdateRequest req). NO field renames. NO MapStruct (manual mapping for clarity). EVERY field that exists in the entity OR DTO MUST appear in the mapping — partial mappers are rejected.",
        "python": "Generate a <Resource>Mapper class with @staticmethod to_dto(entity) + to_entity(payload) + update_entity(entity, payload). Explicit field assignments — NO `**dict` blind copy. Field names match OLTP columns verbatim. Wire datetime/decimal/enum coercions explicitly.",
        "any":    "Generate a Mapper that converts between persistence entities and API DTOs. Explicit field-by-field mappings; no implicit copies; field names match the OLTP DDL verbatim.",
    },
    "validator": {
        "java":   "Generate a @Component <Resource>Validator class with ONE validate<Method>(<Resource>Dtos.<Req> r) method per state-changing endpoint. EACH validation MUST come from KB CONTEXT.validation_rules above — use the EXACT error_code and message. Throw <Resource>Exception.BadRequest(code, message) on violation. Cross-field rules, regex, range, enum, FK existence checks ALL belong here (NOT in @Valid annotations alone). Bean-Validation annotations on DTOs are necessary but NOT sufficient — this class is the BR enforcement gate.",
        "python": "Generate a <Resource>Validator class with one validate_<method>(payload: <Resource>Schema, db: AsyncSession) per state-changing endpoint. EVERY rule from KB CONTEXT.validation_rules MUST be enforced with its EXACT error_code. Raise <Resource>Exception.BadRequest(code, message) on violation. Cross-field rules, async FK existence checks, role-based field-level write guards belong here.",
        "any":    "Generate a Validator class that enforces every business-rule from KB CONTEXT.validation_rules; raise <Resource>Exception with the exact legacy error_code.",
    },
    "exception": {
        "java":   "Generate a `public class <Resource>Exception extends RuntimeException` with a `String code` field. Nested public-static subclasses: NotFound (404), Conflict (409), BadRequest (400), Forbidden (403), Unprocessable (422). Each subclass accepts (String code, String message) and forwards to the parent. The GlobalExceptionHandler maps these to ErrorResponse. NO stack-trace exposure to the API surface.",
        "python": "Generate `class <Resource>Exception(Exception):` with subclasses NotFound, Conflict, BadRequest, Forbidden, Unprocessable. Each takes (code: str, message: str). FastAPI exception_handlers in app/main.py map these to the standard error envelope.",
        "any":    "Generate a per-domain Exception hierarchy: <Resource>Exception base + NotFound / Conflict / BadRequest / Forbidden / Unprocessable subclasses. Each carries a `code` field set to the legacy error_code (verbatim).",
    },
    "controller": {
        "nodejs": "Generate an Express Router for ONE resource. Implement EVERY endpoint listed in `API ENDPOINTS` verbatim - no skips, no consolidation. Zod request validation, role-guard middleware reused, structured error envelope. Each handler is a thin pass-through to the matching Service method.",
        "python": "Generate a FastAPI APIRouter for ONE resource. Implement EVERY endpoint listed in `API ENDPOINTS` verbatim - no skips. Pydantic request/response models, dependency-injected service + auth/role, structured error envelope.",
        "java":   "Generate a Spring @RestController for ONE resource. Implement EVERY endpoint listed in `API ENDPOINTS` verbatim - no skips. @PreAuthorize role checks, @Valid request DTOs, ResponseEntity returns, ProblemDetail / ErrorResponse on errors. Each method is a thin pass-through to the Service.",
        "go":     "Generate a Gin handler for ONE resource. Implement EVERY endpoint listed in `API ENDPOINTS` verbatim - no skips.",
        "dotnet": "Generate an ASP.NET Core 8 [ApiController] for ONE resource. Implement EVERY endpoint in `API ENDPOINTS` verbatim - no skips. [Authorize(Roles=...)] per legacy roles, FluentValidation, ActionResult<T>, ProblemDetails on errors.",
        "php":    "Generate a Laravel Controller for ONE resource. Implement EVERY endpoint in `API ENDPOINTS` verbatim - no skips.",
        "ruby":   "Generate a Rails ApiController for ONE resource. Implement EVERY endpoint in `API ENDPOINTS` verbatim.",
        "rust":   "Generate Axum handler functions for ONE resource. Implement EVERY endpoint in `API ENDPOINTS` verbatim.",
    },
    "service": {
        "nodejs": "Generate a Service class for ONE resource. 100% of legacy business logic preserved. JSDoc with `SRS:` and `LEGACY:` tags.",
        "python": "Generate a Service class for ONE resource. 100% of legacy business logic preserved. Docstrings with `SRS:` / `LEGACY:` tags.",
        "java":   "Generate a @Service class for ONE resource. 100% of legacy business logic preserved verbatim. @Transactional where the legacy code is transactional. Javadoc with `SRS:` and `LEGACY:` tags on every public method.",
        "go":     "Generate a service struct + interface for ONE resource. 100% of legacy business logic preserved.",
        "dotnet": "Generate a Service class (interface + impl) for ONE resource. 100% legacy business logic preserved.",
        "php":    "Generate a Laravel Service class for ONE resource. 100% legacy business logic preserved.",
        "ruby":   "Generate a Rails Service Object for ONE resource. 100% legacy business logic preserved.",
        "rust":   "Generate a service struct (with trait) for ONE resource. 100% legacy business logic preserved.",
    },
    "repository": {
        "nodejs": "Generate a Repository class using `pg` parameterised queries. Column names match OLTP DDL verbatim.",
        "python": "Generate a SQLAlchemy 2.0 async Repository. Column names verbatim from OLTP DDL.",
        "java":   "Generate a Spring Data JPA repository INTERFACE extending JpaRepository<Entity, Long>. @Query (JPQL) methods for every legacy custom query. NEVER string-interpolated SQL.",
        "go":     "Generate a Repository using sqlx. Parameterised queries.",
        "dotnet": "Generate an EF Core 8 Repository (interface + impl).",
        "php":    "Generate a Laravel Eloquent-backed Repository.",
        "ruby":   "Generate an ActiveRecord scope/query class.",
        "rust":   "Generate a sqlx-backed Repository. Parameterised query macros only.",
    },
    "entity": {
        "nodejs": "Generate an ORM/POJO file. Fields match every column in OLTP DDL VERBATIM.",
        "python": "Generate a SQLAlchemy 2.0 ORM model. Field names match OLTP DDL VERBATIM.",
        "java":   "Generate a JPA @Entity class. @Entity + @Table(name=\"<legacy_table>\"). One @Column field per OLTP column (verbatim name). Bean Validation annotations. Getter/setter for every field.",
        "go":     "Generate a struct with `db` tags matching OLTP column names verbatim.",
        "dotnet": "Generate an EF Core entity class. PascalCase properties pinned to OLTP column names via [Column].",
        "php":    "Generate an Eloquent model.",
        "ruby":   "Generate an ActiveRecord model.",
        "rust":   "Generate a sqlx::FromRow struct.",
    },
    "dto": {
        "nodejs": "Generate Zod schemas (request + response) for endpoints in `API ENDPOINTS`.",
        "python": "Generate Pydantic v2 BaseModels (request + response) for endpoints in `API ENDPOINTS`.",
        "java":   "Generate Java records (request + response DTOs) wrapped in ONE public final class named <Resource>Dtos. Bean Validation annotations on request records.",
        "go":     "Generate request/response structs for endpoints.",
        "dotnet": "Generate request/response record types (C# 12) for endpoints.",
        "php":    "Generate FormRequest + Resource classes per endpoint.",
        "ruby":   "Generate Strong Params + Serializers per endpoint.",
        "rust":   "Generate request/response structs deriving Deserialize / Serialize / Validate.",
    },
    "errors": {
        "nodejs": "Generate centralised Express error middleware + AppError class.",
        "python": "Generate exception_handler set for FastAPI + AppError.",
        "java":   "Generate @RestControllerAdvice GlobalExceptionHandler + ErrorResponse record + AppException class - ALL in this single file. @ExceptionHandler for AppException + Exception fallback.",
        "go":     "Generate AppError type + middleware rendering standard JSON error envelope.",
        "dotnet": "Generate exception-handling middleware + ProblemDetails-derived ErrorResponse.",
        "php":    "Generate Laravel app/Exceptions/Handler.php + AppException class.",
        "ruby":   "Generate rescue_from set in ApplicationController + AppError class.",
        "rust":   "Generate AppError enum implementing IntoResponse.",
    },
    "security": {
        "nodejs": "Generate JWT verify middleware + role-gate factory `requireRole(...roles)`.",
        "python": "Generate FastAPI dependencies: get_current_user (JWT) + require_roles(...).",
        "java":   "Generate a SecurityConfig @Configuration class (Spring Security 6, lambda DSL) exposing a @Bean SecurityFilterChain. JWT resource server, @EnableMethodSecurity, role hierarchy.",
        "go":     "Generate JWT auth middleware + role-check middleware.",
        "dotnet": "Generate JwtBearer + Authorization policy registration (AuthConfig.cs).",
        "php":    "Generate Laravel auth + Gates/Policies aligned to legacy roles.",
        "ruby":   "Generate Devise / JWT setup + Pundit policies aligned to legacy roles.",
        "rust":   "Generate JWT extractor + role-check extractor (Axum).",
    },
    "model": {
        "nodejs": "Generate a Sequelize ORM model file.",
        "python": "Generate a SQLAlchemy 2.0 ORM model.",
        "java":   "Generate a JPA @Entity class.",
        "go":     "Generate a GORM model struct.",
        "dotnet": "Generate an EF Core 8 entity class.",
        "php":    "Generate a Laravel 11 Eloquent model.",
        "ruby":   "Generate a Rails 7.2 ActiveRecord model.",
        "rust":   "Generate a sqlx::FromRow struct.",
    },
    "route": {
        "nodejs": "Generate an Express Router covering EVERY endpoint in `API ENDPOINTS`.",
        "python": "Generate a FastAPI router covering EVERY endpoint in `API ENDPOINTS`.",
        "java":   "Generate a Spring @RestController covering EVERY endpoint in `API ENDPOINTS`.",
        "go":     "Generate a Gin route handler covering EVERY endpoint.",
        "dotnet": "Generate an ASP.NET Core 8 Controller covering EVERY endpoint.",
        "php":    "Generate a Laravel 11 Controller covering EVERY endpoint.",
        "ruby":   "Generate a Rails 7.2 ApiController covering EVERY endpoint.",
        "rust":   "Generate Axum route handlers covering EVERY endpoint.",
    },
    "middleware": {
        "nodejs": "Express middleware: JWT verify, role check, request logging, rate limiting.",
        "python": "FastAPI dependencies + middleware: JWT auth, role check, logging.",
        "java":   "Spring Security configuration + filter chain.",
        "go":     "Gin middleware functions.",
        "dotnet": "ASP.NET Core middleware.",
        "php":    "Laravel middleware.",
        "ruby":   "Rails before_action filters + Rack middleware.",
        "rust":   "Axum middleware/layer.",
    },
    "dockerfile": {
        "nodejs": "Multi-stage Dockerfile: node:20-alpine builder + runtime, non-root, healthcheck.",
        "python": "Multi-stage Dockerfile: python:3.12-slim builder + runtime, non-root, healthcheck.",
        "java":   "Multi-stage Dockerfile: eclipse-temurin:21-jdk-alpine + jre-alpine, healthcheck.",
        "go":     "Multi-stage Dockerfile: golang:1.22-alpine + alpine, non-root, healthcheck.",
        "dotnet": "Multi-stage Dockerfile: dotnet/sdk:8.0 + aspnet:8.0, non-root, healthcheck.",
        "php":    "Multi-stage Dockerfile: composer:2 + php:8.3-fpm-alpine + nginx, healthcheck.",
        "ruby":   "Multi-stage Dockerfile: ruby:3.3-alpine, non-root, RAILS_ENV=production.",
        "rust":   "Multi-stage Dockerfile: rust:1.79-slim + distroless/cc-debian12.",
    },
    "test": {
        "nodejs": "Jest tests covering happy + error + edge for EVERY endpoint.",
        "python": "Pytest tests with fixtures, covering every endpoint.",
        "java":   "JUnit 5 + Mockito + @WebMvcTest / @SpringBootTest. Cover happy + error + role-denied for EVERY endpoint.",
        "go":     "Testify table-driven tests for every endpoint.",
        "dotnet": "xUnit + Moq + WebApplicationFactory<Program>.",
        "php":    "PHPUnit Feature + Unit tests.",
        "ruby":   "RSpec request specs + service specs.",
        "rust":   "cargo test integration tests.",
    },
    "config": {
        "nodejs": ".env.example + config/database.js + config/app.js.",
        "python": ".env.example + config/settings.py (Pydantic BaseSettings) + config/database.py.",
        "java":   "application.yml + application-test.yml.",
        "go":     "config/config.go (viper) + .env.example.",
        "dotnet": "appsettings.json + appsettings.Development.json + .env.example.",
        "php":    "config/database.php + config/app.php + .env.example.",
        "ruby":   "config/database.yml + config/application.rb + .env.example.",
        "rust":   "config.toml + Settings struct (config crate) + .env.example.",
    },
    "compose": {"any": "docker-compose.yml linking all services + PostgreSQL + Redis. Healthchecks. Named volumes for DB. Environment via .env file."},
    "ci":      {"any": "GitHub Actions workflow: install, lint, test, build matrix per service. Run on push + pull_request."},
}


FRONTEND_COMPONENT_INSTRUCTIONS = {
    "page": "Full page for the named SRS use case. Components: data fetch (loading skeleton + error + empty), forms with full legacy validation parity, role-aware visibility via <RoleGate>, breadcrumbs, modals for create/edit, pagination, accessibility primitives.",
    "form": "React Hook Form + Zod schema. Field-level errors mirroring legacy messages.",
    "table": "Sortable, filterable, paginated data table with row actions and bulk select.",
    "layout": "Sidebar nav + breadcrumbs + user menu + notifications + responsive mobile menu.",
    "api_client": "Typed axios client. One function per `operationId` in the API CONTRACTS slice above (EVERY operationId - no skips).",
    "store": "Zustand store: typed state, actions, selectors. Persist auth token + user/role.",
    "route_config": "Lazy-loaded routes - one entry per page. Protected routes via auth guard. Role-based guards.",
    "role_guard": "Generate a <RoleGate roles={{['ROLE_A','ROLE_B']}}> component that hides children when the current user is not in the allowed role set.",
    "scaffold": "package.json + vite.config.ts + tsconfig.json + index.html + main.tsx for a fresh Vite app.",
    "bootstrap": "Generate the framework's entrypoint file. Mount the App, register router + state + theme + auth bootstrap.",
}


# ---------------------------------------------------------------------------
# iter-13.82 - Concrete annotated skeletons. Few-shot beats prose.
# ---------------------------------------------------------------------------
_SKEL_CTRL_JAVA = _tw.dedent("""\
    package com.lama.<service>.web;

    import com.lama.<service>.dto.<Resource>Dtos.*;
    import com.lama.<service>.service.<Resource>Service;
    import jakarta.validation.Valid;
    import org.slf4j.Logger;
    import org.slf4j.LoggerFactory;
    import org.springframework.http.ResponseEntity;
    import org.springframework.security.access.prepost.PreAuthorize;
    import org.springframework.web.bind.annotation.*;

    import java.util.List;

    /**
     * REST entry point for the <Resource> resource.
     * SRS: UC-<NN>
     * LEGACY: <LegacyClass>.<method>
     */
    @RestController
    @RequestMapping("/api/v1/<resources>")
    public class <Resource>Controller {

        private static final Logger log = LoggerFactory.getLogger(<Resource>Controller.class);
        private final <Resource>Service service;

        public <Resource>Controller(<Resource>Service service) {
            this.service = service;
        }

        /** SRS: UC-<NN>; LEGACY: <LegacyClass>.<method> */
        @GetMapping("/{id}")
        @PreAuthorize("hasAnyRole(<LEGACY_ROLES>)")
        public ResponseEntity<Response> get(@PathVariable String id) {
            log.info("op=get id={}", id);
            return ResponseEntity.ok(service.get(id));
        }

        // ONE annotated method PER endpoint listed in API ENDPOINTS - no skips.
    }
""")

_SKEL_SVC_JAVA = _tw.dedent("""\
    package com.lama.<service>.service;

    import com.lama.<service>.domain.<Resource>;
    import com.lama.<service>.repo.<Resource>Repository;
    import com.lama.<service>.web.AppException;
    import org.slf4j.Logger;
    import org.slf4j.LoggerFactory;
    import org.springframework.stereotype.Service;
    import org.springframework.transaction.annotation.Transactional;

    /**
     * Business logic for <Resource>. Reproduces legacy branch-for-branch.
     * SRS: UC-<NN>
     * LEGACY: <LegacyClass>
     */
    @Service
    public class <Resource>Service {

        private static final Logger log = LoggerFactory.getLogger(<Resource>Service.class);
        private final <Resource>Repository repository;

        public <Resource>Service(<Resource>Repository repository) {
            this.repository = repository;
        }

        /**
         * Preconditions:
         *   1. <real precondition #1 from LEGACY EVIDENCE>
         * Postconditions:
         *   1. <observable state mutation>
         * Business Rules:
         *   - BR-<ID>: <verbatim legacy rule text from SRS>
         * SRS: UC-<NN>
         * LEGACY: <LegacyClass>.<method>
         */
        @Transactional
        public <Resource> doSomething(<Resource> input) {
            // 1. validate  2. authorise  3. mutate (DB / side-effects)  4. emit (audit-log / notification)
            return repository.save(input);
        }
    }
""")

_SKEL_REPO_JAVA = _tw.dedent("""\
    package com.lama.<service>.repo;

    import com.lama.<service>.domain.<Resource>;
    import org.springframework.data.jpa.repository.JpaRepository;
    import org.springframework.data.jpa.repository.Query;
    import org.springframework.data.repository.query.Param;
    import org.springframework.stereotype.Repository;

    import java.util.List;
    import java.util.Optional;

    @Repository
    public interface <Resource>Repository extends JpaRepository<<Resource>, Long> {

        Optional<<Resource>> findByNaturalKey(@Param("key") String key);

        /** LEGACY: <LegacyDao>.<method> */
        @Query("SELECT e FROM <Resource> e WHERE e.<column> = :value")
        List<<Resource>> findByLegacyPredicate(@Param("value") String value);
    }
""")

_SKEL_ENT_JAVA = _tw.dedent("""\
    package com.lama.<service>.domain;

    import jakarta.persistence.*;
    import jakarta.validation.constraints.*;

    import java.time.OffsetDateTime;

    /** LEGACY: <legacy_table> */
    @Entity
    @Table(name = "<legacy_table>")
    public class <Resource> {

        @Id
        @GeneratedValue(strategy = GenerationType.IDENTITY)
        private Long id;

        @NotBlank
        @Column(name = "<legacy_column>", nullable = false, length = 200)
        private String name;

        @Column(name = "created_at", nullable = false, updatable = false)
        private OffsetDateTime createdAt;

        // ONE @Column field PER OLTP column - names verbatim.

        public Long getId() { return id; }
        public void setId(Long id) { this.id = id; }
        public String getName() { return name; }
        public void setName(String name) { this.name = name; }
        public OffsetDateTime getCreatedAt() { return createdAt; }
        public void setCreatedAt(OffsetDateTime createdAt) { this.createdAt = createdAt; }
    }
""")

_SKEL_DTO_JAVA = _tw.dedent("""\
    package com.lama.<service>.dto;

    import jakarta.validation.constraints.*;

    public final class <Resource>Dtos {
        private <Resource>Dtos() {}

        public record CreateRequest(
            @NotBlank @Size(max = 200) String name,
            @Email String email
        ) {}

        public record UpdateRequest(
            @Size(max = 200) String name
        ) {}

        public record Response(Long id, String name) {}
    }
""")

_SKEL_ERR_JAVA = _tw.dedent("""\
    package com.lama.<service>.web;

    import org.springframework.http.*;
    import org.springframework.web.bind.annotation.*;

    public record ErrorResponse(boolean error, String code, String message, String traceId) {}

    @RestControllerAdvice
    class GlobalExceptionHandler {

        @ExceptionHandler(AppException.class)
        public ResponseEntity<ErrorResponse> appException(AppException ex) {
            return ResponseEntity.status(ex.status()).body(
                new ErrorResponse(true, ex.code(), ex.getMessage(), "")
            );
        }

        @ExceptionHandler(Exception.class)
        public ResponseEntity<ErrorResponse> fallback(Exception ex) {
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(
                new ErrorResponse(true, "E_INTERNAL", ex.getMessage(), "")
            );
        }
    }

    class AppException extends RuntimeException {
        private final HttpStatus status;
        private final String code;
        public AppException(HttpStatus status, String code, String message) {
            super(message);
            this.status = status;
            this.code = code;
        }
        public HttpStatus status() { return status; }
        public String code() { return code; }
    }
""")

_SKEL_SEC_JAVA = _tw.dedent("""\
    package com.lama.<service>.config;

    import org.springframework.context.annotation.*;
    import org.springframework.security.config.annotation.method.configuration.EnableMethodSecurity;
    import org.springframework.security.config.annotation.web.builders.HttpSecurity;
    import org.springframework.security.config.http.SessionCreationPolicy;
    import org.springframework.security.web.SecurityFilterChain;

    @Configuration
    @EnableMethodSecurity
    public class SecurityConfig {

        @Bean
        public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
            http
                .csrf(csrf -> csrf.disable())
                .sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(a -> a
                    .requestMatchers("/actuator/health", "/actuator/info").permitAll()
                    .anyRequest().authenticated())
                .oauth2ResourceServer(o -> o.jwt(j -> {}));
            return http.build();
        }
    }
""")

_SKEL_BOOT_JAVA = _tw.dedent("""\
    package com.lama.<service>;

    import org.springframework.boot.SpringApplication;
    import org.springframework.boot.autoconfigure.SpringBootApplication;

    @SpringBootApplication
    public class Application {
        public static void main(String[] args) {
            SpringApplication.run(Application.class, args);
        }
    }
""")

_SKEL_CTRL_PY = _tw.dedent('''\
    from fastapi import APIRouter, Depends, HTTPException, status
    from typing import List

    from app.dto.<resource>_dto import <Resource>CreateRequest, <Resource>Response
    from app.service.<resource>_service import <Resource>Service
    from app.security.deps import get_current_user, require_roles

    router = APIRouter(prefix="/api/v1/<resources>", tags=["<resources>"])


    @router.get("/{id}", response_model=<Resource>Response)
    async def get(
        id: str,
        service: <Resource>Service = Depends(),
        user = Depends(require_roles(<LEGACY_ROLES>)),
    ):
        """SRS: UC-<NN>; LEGACY: <LegacyClass>.<method>"""
        return await service.get(id)
''')

_SKEL_SVC_PY = _tw.dedent('''\
    import logging
    from app.repo.<resource>_repo import <Resource>Repo

    logger = logging.getLogger(__name__)


    class <Resource>Service:
        """Business logic for <Resource>. Reproduces legacy branch-for-branch.

        SRS: UC-<NN>
        LEGACY: <LegacyClass>
        """

        def __init__(self, repo: <Resource>Repo):
            self.repo = repo

        async def do_something(self, payload):
            """Preconditions:
                1. <real precondition #1 from LEGACY EVIDENCE>
            Postconditions:
                1. <observable state mutation>
            Business Rules:
                - BR-<ID>: <verbatim legacy rule text>
            SRS: UC-<NN>
            LEGACY: <LegacyClass>.<method>
            """
            return await self.repo.save(payload)
''')

_SKEL_CTRL_NET = _tw.dedent('''\
    using Microsoft.AspNetCore.Authorization;
    using Microsoft.AspNetCore.Mvc;
    using <Project>.Application.Services;
    using <Project>.Contracts;

    namespace <Project>.Web.Controllers;

    [ApiController]
    [Route("api/v1/<resources>")]
    public class <Resource>Controller : ControllerBase
    {
        private readonly I<Resource>Service _service;
        private readonly ILogger<<Resource>Controller> _log;

        public <Resource>Controller(I<Resource>Service service, ILogger<<Resource>Controller> log)
        {
            _service = service;
            _log = log;
        }

        /// <summary>SRS: UC-<NN>; LEGACY: <LegacyClass>.<method></summary>
        [HttpGet("{id}")]
        [Authorize(Roles = "<LEGACY_ROLES>")]
        public async Task<ActionResult<<Resource>Response>> Get(string id)
        {
            _log.LogInformation("op=get id={Id}", id);
            var result = await _service.GetAsync(id);
            return Ok(result);
        }
    }
''')

_SKEL_CTRL_NODE = _tw.dedent("""\
    import { Router } from 'express';
    import { z } from 'zod';
    import { asyncHandler } from '../middleware/asyncHandler.js';
    import { requireRoles } from '../middleware/requireRoles.js';
    import * as service from '../service/<resource>Service.js';

    const router = Router();
    const getParams = z.object({ id: z.string().min(1) });

    /** SRS: UC-<NN>; LEGACY: <LegacyClass>.<method> */
    router.get('/:id', requireRoles(['<LEGACY_ROLE>']), asyncHandler(async (req, res) => {
        const { id } = getParams.parse(req.params);
        const result = await service.get(id);
        res.json(result);
    }));

    export default router;
""")

# ── iter-13.115 — production-grade per-resource split skeletons ─────────────
_SKEL_MAPPER_JAVA = _tw.dedent("""\
    package com.lama.<svc>.mapper;

    import com.lama.<svc>.domain.<Resource>;
    import com.lama.<svc>.dto.<Resource>Dtos;
    import org.springframework.stereotype.Component;

    /**
     * <Resource> entity <-> DTO mapper. Manual field-by-field mapping —
     * NO MapStruct, NO BeanUtils.copyProperties. Column names match the
     * frozen OLTP DDL verbatim.
     *
     * SRS: <UC-ID>
     * LEGACY: <legacy mapper / DAO class>
     */
    @Component
    public class <Resource>Mapper {

        public <Resource>Dtos.Response toDto(<Resource> entity) {
            if (entity == null) return null;
            return new <Resource>Dtos.Response(
                entity.getId(),
                entity.get<Field1>(),
                entity.get<Field2>()
                // ... map EVERY column from the OLTP DDL
            );
        }

        public <Resource> toEntity(<Resource>Dtos.CreateRequest req) {
            <Resource> e = new <Resource>();
            e.set<Field1>(req.<field1>());
            e.set<Field2>(req.<field2>());
            return e;
        }

        public void updateEntity(<Resource> entity, <Resource>Dtos.UpdateRequest req) {
            if (req.<field1>() != null) entity.set<Field1>(req.<field1>());
            if (req.<field2>() != null) entity.set<Field2>(req.<field2>());
        }
    }
""")

_SKEL_VALIDATOR_JAVA = _tw.dedent("""\
    package com.lama.<svc>.validation;

    import com.lama.<svc>.dto.<Resource>Dtos;
    import com.lama.<svc>.exception.<Resource>Exception;
    import org.springframework.stereotype.Component;

    /**
     * Business-rule enforcement gate. Every rule from KB CONTEXT
     * .validation_rules MUST be enforced here with its EXACT legacy
     * error_code. Bean-Validation annotations on the DTO cover shape
     * checks only — semantic rules live here.
     *
     * SRS: <UC-ID>, <UC-ID>
     * LEGACY: <legacy validator / form-request class>
     */
    @Component
    public class <Resource>Validator {

        public void validateCreate(<Resource>Dtos.CreateRequest r) {
            // BR-<n>: <rule statement from KB CONTEXT>
            if (/* <predicate> */ false) {
                throw new <Resource>Exception.BadRequest(
                    "<E_CODE_FROM_KB>", "<message_from_KB>"
                );
            }
            // ... one if-throw per validation_rule listed for this method
        }

        public void validateUpdate(<Resource>Dtos.UpdateRequest r) {
            // ... rules scoped to update flow
        }
    }
""")

_SKEL_EXCEPTION_JAVA = _tw.dedent("""\
    package com.lama.<svc>.exception;

    /**
     * Per-domain exception hierarchy. Carries the legacy `code` field
     * VERBATIM so error-envelope round-trips preserve client contracts.
     */
    public class <Resource>Exception extends RuntimeException {
        private final String code;

        public <Resource>Exception(String code, String message) {
            super(message);
            this.code = code;
        }
        public String getCode() { return code; }

        public static class NotFound extends <Resource>Exception {
            public NotFound(String code, String message) { super(code, message); }
        }
        public static class Conflict extends <Resource>Exception {
            public Conflict(String code, String message) { super(code, message); }
        }
        public static class BadRequest extends <Resource>Exception {
            public BadRequest(String code, String message) { super(code, message); }
        }
        public static class Forbidden extends <Resource>Exception {
            public Forbidden(String code, String message) { super(code, message); }
        }
        public static class Unprocessable extends <Resource>Exception {
            public Unprocessable(String code, String message) { super(code, message); }
        }
    }
""")

_SKEL_MAPPER_PY = _tw.dedent("""\
    from app.models.<resource> import <Resource>
    from app.schemas.<resource> import <Resource>Response, <Resource>Create, <Resource>Update


    class <Resource>Mapper:
        '''Entity <-> DTO mapper. Explicit field mapping; field names
        match the OLTP DDL verbatim. SRS: <UC-ID> LEGACY: <legacy ref>'''

        @staticmethod
        def to_dto(entity: <Resource>) -> <Resource>Response:
            return <Resource>Response(
                id=entity.id,
                # field1=entity.field1,
                # ... map EVERY OLTP column
            )

        @staticmethod
        def to_entity(payload: <Resource>Create) -> <Resource>:
            return <Resource>(
                # field1=payload.field1,
                # ...
            )

        @staticmethod
        def update_entity(entity: <Resource>, payload: <Resource>Update) -> None:
            data = payload.model_dump(exclude_unset=True)
            for k, v in data.items():
                setattr(entity, k, v)
""")

_SKEL_VALIDATOR_PY = _tw.dedent("""\
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.schemas.<resource> import <Resource>Create, <Resource>Update
    from app.exceptions.<resource>_exceptions import <Resource>Exception


    class <Resource>Validator:
        '''Business-rule enforcement gate. Every rule from KB CONTEXT
        .validation_rules MUST be enforced with its EXACT error_code.
        SRS: <UC-ID> LEGACY: <legacy ref>'''

        async def validate_create(self, payload: <Resource>Create, db: AsyncSession) -> None:
            # BR-<n>: <rule statement from KB CONTEXT>
            if False:  # <predicate>
                raise <Resource>Exception.BadRequest(
                    code="<E_CODE_FROM_KB>", message="<message_from_KB>"
                )
            # ... one raise per validation_rule for this method

        async def validate_update(self, payload: <Resource>Update, db: AsyncSession) -> None:
            ...
""")

_SKEL_EXCEPTION_PY = _tw.dedent("""\
    class <Resource>Exception(Exception):
        '''Per-domain exception hierarchy. Carries legacy error_code verbatim.'''

        def __init__(self, code: str, message: str):
            super().__init__(message)
            self.code = code
            self.message = message

        class NotFound(Exception):
            def __init__(self, code: str, message: str):
                super().__init__(message)
                self.code = code

        class Conflict(Exception):
            def __init__(self, code: str, message: str):
                super().__init__(message)
                self.code = code

        class BadRequest(Exception):
            def __init__(self, code: str, message: str):
                super().__init__(message)
                self.code = code

        class Forbidden(Exception):
            def __init__(self, code: str, message: str):
                super().__init__(message)
                self.code = code

        class Unprocessable(Exception):
            def __init__(self, code: str, message: str):
                super().__init__(message)
                self.code = code
""")

FILE_TYPE_SKELETONS = {
    ("controller", "java"):   _SKEL_CTRL_JAVA,
    ("service",    "java"):   _SKEL_SVC_JAVA,
    ("repository", "java"):   _SKEL_REPO_JAVA,
    ("entity",     "java"):   _SKEL_ENT_JAVA,
    ("model",      "java"):   _SKEL_ENT_JAVA,
    ("dto",        "java"):   _SKEL_DTO_JAVA,
    ("errors",     "java"):   _SKEL_ERR_JAVA,
    ("security",   "java"):   _SKEL_SEC_JAVA,
    ("bootstrap",  "java"):   _SKEL_BOOT_JAVA,
    ("controller", "python"): _SKEL_CTRL_PY,
    ("service",    "python"): _SKEL_SVC_PY,
    ("controller", "dotnet"): _SKEL_CTRL_NET,
    ("controller", "nodejs"): _SKEL_CTRL_NODE,
    # iter-13.115 — production-grade per-resource split
    ("mapper",     "java"):   _SKEL_MAPPER_JAVA,
    ("validator",  "java"):   _SKEL_VALIDATOR_JAVA,
    ("exception",  "java"):   _SKEL_EXCEPTION_JAVA,
    ("mapper",     "python"): _SKEL_MAPPER_PY,
    ("validator",  "python"): _SKEL_VALIDATOR_PY,
    ("exception",  "python"): _SKEL_EXCEPTION_PY,
}


# ---------------------------------------------------------------------------
# iter-13.85 - Frontend skeletons + required-tokens. Keyed by
# (component_type, framework). Without these, frontend files bypassed
# structural validation entirely (validator was keyed on backend_lang,
# which is empty for frontend services) and the LLM was free to ship
# malformed .tsx files.
# ---------------------------------------------------------------------------
_SKEL_PAGE_REACT = _tw.dedent("""\
    import { useQuery } from '@tanstack/react-query';
    import { Link } from 'react-router-dom';
    import { useState } from 'react';
    import { client } from '@/api/client';
    import { RoleGate } from '@/components/RoleGate';

    /**
     * <Resource> page.
     * SRS: UC-<NN>
     * LEGACY: <legacy_template>
     */
    export default function <Resource>Page() {
        const [query, setQuery] = useState('');
        const { data, isLoading, isError, error } = useQuery({
            queryKey: ['<resources>', query],
            queryFn: () => client.get('/api/v1/<resources>', { params: { query } }).then(r => r.data),
        });

        if (isLoading) return <div role="status" aria-live="polite">Loading…</div>;
        if (isError)   return <div role="alert">{(error as Error).message}</div>;
        if (!data?.length) return <div>No <resources> yet.</div>;

        return (
            <section aria-labelledby="<resource>-heading">
                <h1 id="<resource>-heading"><Resource>s</h1>
                <label>
                    Search
                    <input value={query} onChange={(e) => setQuery(e.target.value)} />
                </label>
                <RoleGate roles={['<LEGACY_ROLE>']}>
                    <Link to="/<resources>/new">Create</Link>
                </RoleGate>
                <ul>
                    {data.map((row: any) => (
                        <li key={row.id}><Link to={`/<resources>/${row.id}`}>{row.name}</Link></li>
                    ))}
                </ul>
            </section>
        );
    }
""")

_SKEL_API_CLIENT_REACT = _tw.dedent("""\
    import axios, { AxiosError } from 'axios';

    const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api';

    export const client = axios.create({
        baseURL: BASE_URL,
        timeout: 15_000,
        withCredentials: true,
    });

    client.interceptors.request.use((cfg) => {
        const token = localStorage.getItem('lama:auth:token');
        if (token) cfg.headers.Authorization = `Bearer ${token}`;
        return cfg;
    });

    client.interceptors.response.use(
        (r) => r,
        (err: AxiosError) => {
            if (err.response?.status === 401) {
                localStorage.removeItem('lama:auth:token');
                window.location.href = '/login';
            }
            return Promise.reject(err);
        },
    );

    export default client;
""")

_SKEL_ROLE_GUARD_REACT = _tw.dedent("""\
    import { ReactNode } from 'react';
    import { useAuthStore } from '@/store/auth';

    type RoleGateProps = { roles: string[]; children: ReactNode; fallback?: ReactNode };

    /** Hides `children` from users whose roles do not intersect `roles`. */
    export function RoleGate({ roles, children, fallback = null }: RoleGateProps) {
        const userRoles = useAuthStore((s) => s.user?.roles ?? []);
        const allowed = roles.some((r) => userRoles.includes(r));
        return <>{allowed ? children : fallback}</>;
    }

    export default RoleGate;
""")

_SKEL_ROUTE_CONFIG_REACT = _tw.dedent("""\
    import { lazy, Suspense } from 'react';
    import { createBrowserRouter, RouterProvider, Navigate } from 'react-router-dom';
    import { useAuthStore } from '@/store/auth';

    const Dashboard = lazy(() => import('@/pages/Dashboard'));

    function Protected({ children }: { children: JSX.Element }) {
        const token = useAuthStore((s) => s.token);
        return token ? children : <Navigate to="/login" replace />;
    }

    const router = createBrowserRouter([
        { path: '/', element: <Protected><Suspense fallback={null}><Dashboard /></Suspense></Protected> },
    ]);

    export default function AppRoutes() {
        return <RouterProvider router={router} />;
    }
""")

_SKEL_STORE_REACT = _tw.dedent("""\
    import { create } from 'zustand';
    import { persist } from 'zustand/middleware';

    type AuthUser = { id: string; name: string; roles: string[] };
    type AuthState = {
        token: string | null;
        user: AuthUser | null;
        setSession: (token: string, user: AuthUser) => void;
        clear: () => void;
    };

    export const useAuthStore = create<AuthState>()(
        persist(
            (set) => ({
                token: null,
                user: null,
                setSession: (token, user) => set({ token, user }),
                clear: () => set({ token: null, user: null }),
            }),
            { name: 'lama:auth' },
        ),
    );
""")

_SKEL_BOOT_REACT = _tw.dedent("""\
    import React from 'react';
    import ReactDOM from 'react-dom/client';
    import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
    import AppRoutes from './routes';
    import './index.css';

    const qc = new QueryClient();

    ReactDOM.createRoot(document.getElementById('root')!).render(
        <React.StrictMode>
            <QueryClientProvider client={qc}>
                <AppRoutes />
            </QueryClientProvider>
        </React.StrictMode>,
    );
""")

FRONTEND_FILE_TYPE_SKELETONS = {
    ("page",         "react"): _SKEL_PAGE_REACT,
    ("api_client",   "react"): _SKEL_API_CLIENT_REACT,
    ("role_guard",   "react"): _SKEL_ROLE_GUARD_REACT,
    ("route_config", "react"): _SKEL_ROUTE_CONFIG_REACT,
    ("store",        "react"): _SKEL_STORE_REACT,
    ("bootstrap",    "react"): _SKEL_BOOT_REACT,
}

FRONTEND_REQUIRED_TOKENS = {
    # React + TypeScript + Vite + React Query + Zustand + react-router-dom
    ("page",         "react"): ["export default", r".regex/use(State|Effect|Query)\b",
                                r".regex/return\s*\("],
    ("api_client",   "react"): [r".regex/axios|fetch", "export"],
    ("role_guard",   "react"): ["children", r".regex/RoleGate|Gate\b"],
    ("route_config", "react"): [r".regex/createBrowserRouter|<Routes|<Route\s"],
    ("store",        "react"): [r".regex/create[\(<]|configureStore|createStore"],
    ("bootstrap",    "react"): [r".regex/createRoot|ReactDOM\.render",
                                r".regex/import\s+React"],
    # Angular 18 standalone components
    ("page",         "angular"): [r".regex/@Component\(", "standalone: true",
                                  r".regex/export\s+class\s+\w+Component\b"],
    ("api_client",   "angular"): ["@Injectable", "HttpClient"],
    ("role_guard",   "angular"): [r".regex/CanActivate|canActivate"],
    ("route_config", "angular"): [r".regex/Routes\b|provideRouter"],
    ("bootstrap",    "angular"): ["bootstrapApplication"],
    # Vue 3 + Composition API
    ("page",         "vue"):     [r".regex/<script\s+setup", r".regex/<template>"],
    ("api_client",   "vue"):     [r".regex/axios|fetch", "export"],
    ("role_guard",   "vue"):     [r".regex/defineProps|<slot"],
    ("route_config", "vue"):     [r".regex/createRouter|createWebHistory"],
    ("store",        "vue"):     [r".regex/defineStore"],
    ("bootstrap",    "vue"):     [r".regex/createApp\("],
    # Svelte / SvelteKit
    ("page",         "svelte"):  [r".regex/<script", r".regex/<svelte:|export\s+let"],
    ("bootstrap",    "svelte"):  [r".regex/import\s+App|new\s+App\("],
}



# ---------------------------------------------------------------------------
# iter-13.82 - REQUIRED_TOKENS: server-side structural validator targets.
# Token forms:
#   "raw"           literal substring must appear (case-sensitive)
#   ".regex/<pat>"  Python re.search(pat, content) must match
# A file failing the bucket triggers a retry with feedback naming the
# missing tokens, so the LLM knows exactly which markers it dropped.
# ---------------------------------------------------------------------------
REQUIRED_TOKENS = {
    # Java - Spring Boot 3
    ("controller", "java"):  ["package ", "@RestController", "@RequestMapping",
                              r".regex/public\s+class\s+\w+Controller\b"],
    ("service",    "java"):  ["package ", "@Service",
                              r".regex/public\s+(class|interface)\s+\w+Service\b"],
    ("repository", "java"):  ["package ", r".regex/JpaRepository|@Repository",
                              r".regex/(interface|class)\s+\w+Repository\b"],
    ("entity",     "java"):  ["package ", "@Entity", "@Table",
                              r".regex/public\s+class\s+\w+\b"],
    ("model",      "java"):  ["package ", "@Entity",
                              r".regex/public\s+class\s+\w+\b"],
    ("dto",        "java"):  ["package ",
                              r".regex/record\s+\w+(Request|Response)\b|class\s+\w+Dtos?\b"],
    ("errors",     "java"):  ["package ", "@RestControllerAdvice", "@ExceptionHandler"],
    ("security",   "java"):  ["package ", "@Configuration", "SecurityFilterChain"],
    ("bootstrap",  "java"):  ["package ", "@SpringBootApplication", "SpringApplication.run"],
    ("test",       "java"):  ["package ", r".regex/@(SpringBootTest|WebMvcTest|DataJpaTest)", "@Test"],
    # Python - FastAPI + SQLAlchemy 2.0
    ("controller", "python"): ["APIRouter", r".regex/@router\.(get|post|put|patch|delete)"],
    ("service",    "python"): [r".regex/class\s+\w+Service\b"],
    ("repository", "python"): [r".regex/class\s+\w+(Repo|Repository)\b"],
    ("entity",     "python"): [r".regex/class\s+\w+\b.*(Base|BaseModel|DeclarativeBase)"],
    ("bootstrap",  "python"): ["FastAPI(", r".regex/@app\.get\(['\"]/health"],
    # NodeJS - Express
    ("controller", "nodejs"): [r".regex/Router\s*\(\)",
                               r".regex/router\.(get|post|put|patch|delete)",
                               "export default router"],
    ("bootstrap",  "nodejs"): ["express(", r".regex/app\.listen\("],
    # .NET - ASP.NET Core
    ("controller", "dotnet"): ["[ApiController]", "[Route(",
                               r".regex/public\s+class\s+\w+Controller\b"],
    ("service",    "dotnet"): [r".regex/public\s+(class|interface)\s+\w+Service\b"],
    ("bootstrap",  "dotnet"): ["WebApplication.CreateBuilder", "app.MapControllers"],
    # Go - Gin
    ("controller", "go"):     ["package ", r".regex/func\s+\w+Handler\(|gin\.Context"],
    ("bootstrap",  "go"):     ["package main", r".regex/func\s+main\("],
    # PHP - Laravel
    ("controller", "php"):    ["<?php", r".regex/class\s+\w+Controller\b", "extends Controller"],
    # Ruby - Rails
    ("controller", "ruby"):   [r".regex/class\s+\w+Controller\s*<\s*A(pi|pplication)Controller"],
    # Rust - Axum
    ("controller", "rust"):   [r".regex/async\s+fn\s+\w+", r".regex/use\s+axum"],
    # ── iter-13.115 — per-resource production-grade split ────────────────
    ("mapper",    "java"):   ["package ", "@Component",
                              r".regex/class\s+\w+Mapper\b",
                              r".regex/toDto\s*\(", r".regex/toEntity\s*\("],
    ("mapper",    "python"): [r".regex/class\s+\w+Mapper\b",
                              r".regex/def\s+to_dto\s*\(",
                              r".regex/def\s+to_entity\s*\("],
    ("validator", "java"):   ["package ", "@Component",
                              r".regex/class\s+\w+Validator\b",
                              r".regex/(throw\s+new\s+\w+Exception|raise\s+\w+Exception)"],
    ("validator", "python"): [r".regex/class\s+\w+Validator\b",
                              r".regex/def\s+validate_\w+\s*\(",
                              r".regex/raise\s+\w+Exception"],
    ("exception", "java"):   ["package ",
                              r".regex/class\s+\w+Exception\s+extends\s+RuntimeException",
                              r".regex/(NotFound|BadRequest|Conflict)"],
    ("exception", "python"): [r".regex/class\s+\w+Exception\s*\(\s*Exception\s*\)",
                              r".regex/class\s+(NotFound|BadRequest|Conflict)"],
}


def get_file_instruction(file_type: str, backend_lang: str) -> str:
    """Per-(file_type, backend_lang) instruction PLUS a concrete annotated
    skeleton when one exists. Few-shot beats prose: a literal skeleton
    showing imports + annotations + class shape is the single most
    effective way to stop the LLM from emitting Java without
    @RestController / @Service, .NET without [ApiController], etc.
    """
    bucket = FILE_TYPE_INSTRUCTIONS.get(file_type, {})
    instr = (
        bucket.get(backend_lang)
        or bucket.get("any")
        or "Generate appropriate content for this file."
    )
    skeleton = FILE_TYPE_SKELETONS.get((file_type, backend_lang))
    if skeleton:
        instr = (
            f"{instr}\n\n"
            f"MANDATORY SKELETON - use exactly this shape; replace `<...>` placeholders\n"
            f"with real values from the inputs; add real method bodies derived from\n"
            f"LEGACY CODE EVIDENCE + SRS; NEVER remove the framework annotations /\n"
            f"imports / package declaration / class header:\n"
            f"```{backend_lang}\n{skeleton}```\n"
            f"The post-generation validator REJECTS files missing the required\n"
            f"framework markers and the next retry will name exactly which marker\n"
            f"was missing. Always emit the markers."
        )
    return instr


def get_frontend_instruction(component_type: str, framework: str = "react") -> str:
    """Per-(component_type, framework) instruction PLUS a concrete
    annotated skeleton when one exists. iter-13.85 — framework parameter
    is new; defaults to react for back-compat with older call sites."""
    base = FRONTEND_COMPONENT_INSTRUCTIONS.get(
        component_type,
        "Generate the file for this path using the target frontend framework idiom.",
    )
    skel = FRONTEND_FILE_TYPE_SKELETONS.get((component_type, (framework or "react").lower()))
    if not skel:
        return base
    return (
        f"{base}\n\n"
        f"MANDATORY SKELETON — use exactly this shape; replace `<...>`\n"
        f"placeholders with real values from API CONTRACTS + LEGACY UI\n"
        f"EVIDENCE + SRS USE CASES; NEVER remove the framework imports,\n"
        f"hooks, or default-export header:\n"
        f"```tsx\n{skel}```\n"
        f"The structural validator REJECTS files missing the mandatory\n"
        f"framework markers and the next retry will name exactly which\n"
        f"marker was missing."
    )


def required_tokens_for(file_type: str, backend_lang: str) -> list:
    """Return tokens / regex-patterns that MUST appear in a generated file
    of the given (file_type, backend_lang). The structural validator in
    ``routes/codegen.py`` uses this to reject malformed output before it
    reaches the user.
    """
    return list(REQUIRED_TOKENS.get((file_type, backend_lang), []))


def frontend_required_tokens_for(component_type: str, framework: str) -> list:
    """iter-13.85 — Frontend equivalent of ``required_tokens_for``.
    Keyed by (component_type, framework) — frontend files have no
    ``backend_lang``, so the backend table was never consulted for them.
    """
    return list(FRONTEND_REQUIRED_TOKENS.get(
        (component_type, (framework or "react").lower()), [],
    ))



