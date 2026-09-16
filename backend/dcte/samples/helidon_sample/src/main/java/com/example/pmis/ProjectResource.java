package com.example.pmis;

import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import jakarta.ws.rs.GET;
import jakarta.ws.rs.POST;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.PathParam;
import jakarta.ws.rs.QueryParam;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;
import org.eclipse.microprofile.config.inject.ConfigProperty;
import io.helidon.security.annotations.Authenticated;

@Path("/api/projects")
@ApplicationScoped
public class ProjectResource {

    @Inject
    private ProjectService service;

    @Inject
    @ConfigProperty(name = "pmis.default.page.size", defaultValue = "25")
    private int defaultPageSize;

    @GET
    @Produces(MediaType.APPLICATION_JSON)
    @Authenticated
    public Object list(@QueryParam("page") Integer page) {
        return service.list(page == null ? 0 : page, defaultPageSize);
    }

    @GET
    @Path("{id}")
    @Produces(MediaType.APPLICATION_JSON)
    public Object get(@PathParam("id") String id) {
        return service.byId(id);
    }

    @POST
    @Path("{id}/approve")
    public Object approve(@PathParam("id") String id) {
        return service.approve(id);
    }
}
