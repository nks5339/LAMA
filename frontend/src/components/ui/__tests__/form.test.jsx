/**
 * Form primitives.
 *
 * The audit counted 96 raw form controls with no schema and no inline
 * validation — Console alone has 33, and a mistyped provider key there
 * surfaces as the documented "cascade of 401s" three stages downstream
 * rather than as an error at the field.
 *
 * <Field> exists to wire the four things that are easy to forget and
 * invisible when missing: a real label association, aria-invalid,
 * aria-describedby covering BOTH hint and error, and role="alert".
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Field, TextInput, SelectInput, FormErrorSummary } from "@/components/ui/form";

describe("Field", () => {
  it("associates the label with the control", async () => {
    render(
      <Field label="API key" htmlFor="api-key">
        {(a11y) => <TextInput id="api-key" {...a11y} />}
      </Field>
    );
    const input = screen.getByLabelText("API key");
    await userEvent.type(input, "sk-test");
    expect(input).toHaveValue("sk-test");
  });

  it("generates a stable id when the caller supplies none", () => {
    render(
      <Field label="Name">{(a11y) => <TextInput {...a11y} id={a11y.id} />}</Field>
    );
    expect(screen.getByLabelText("Name")).toBeInTheDocument();
  });

  it("marks required fields for both sighted and screen-reader users", () => {
    render(
      <Field label="Deployment" required htmlFor="dep">
        {(a11y) => <TextInput id="dep" {...a11y} />}
      </Field>
    );
    expect(screen.getByText("(required)")).toBeInTheDocument();
    expect(screen.getByLabelText(/deployment/i)).toHaveAttribute("aria-required", "true");
  });

  it("exposes the hint as the accessible description", () => {
    render(
      <Field label="Base URL" hint="Account root only." htmlFor="u">
        {(a11y) => <TextInput id="u" {...a11y} />}
      </Field>
    );
    expect(screen.getByLabelText("Base URL")).toHaveAccessibleDescription(
      "Account root only."
    );
  });

  describe("error state", () => {
    const withError = () =>
      render(
        <Field
          label="API key"
          htmlFor="k"
          hint="Detected from the prefix."
          error="That looks too short for an API key."
        >
          {(a11y) => <TextInput id="k" invalid {...a11y} />}
        </Field>
      );

    it("sets aria-invalid on the control", () => {
      withError();
      expect(screen.getByLabelText("API key")).toHaveAttribute("aria-invalid", "true");
    });

    it("announces the error immediately via role=alert", () => {
      withError();
      expect(screen.getByRole("alert")).toHaveTextContent(/too short/i);
    });

    it("points aria-describedby at the error", () => {
      withError();
      expect(screen.getByLabelText("API key")).toHaveAccessibleDescription(
        /too short/i
      );
    });

    it("replaces the hint rather than stacking two descriptions", () => {
      withError();
      expect(screen.queryByText("Detected from the prefix.")).not.toBeInTheDocument();
    });

    it("styles the border from a token, not a literal", () => {
      withError();
      expect(screen.getByLabelText("API key").className).toContain("border-crit");
    });
  });
});

describe("TextInput / SelectInput", () => {
  it("renders a focus ring from the ring token", () => {
    render(<TextInput aria-label="x" />);
    expect(screen.getByLabelText("x").className).toContain("focus-visible:ring-ring");
  });

  it("renders select options", async () => {
    render(
      <SelectInput aria-label="Provider" defaultValue="">
        <option value="">Auto-detect</option>
        <option value="azure">Azure OpenAI</option>
      </SelectInput>
    );
    await userEvent.selectOptions(screen.getByLabelText("Provider"), "azure");
    expect(screen.getByLabelText("Provider")).toHaveValue("azure");
  });

  it("marks a disabled control as not-allowed rather than just dimming it", () => {
    render(<TextInput aria-label="d" disabled />);
    expect(screen.getByLabelText("d").className).toContain("disabled:cursor-not-allowed");
  });
});

describe("FormErrorSummary", () => {
  const errors = {
    api_key: { message: "Paste an API key." },
    base_url: { message: "Include the scheme." },
  };

  it("renders nothing when the form is valid", () => {
    const { container } = render(<FormErrorSummary errors={{}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("counts the fields that need attention", () => {
    render(<FormErrorSummary errors={errors} />);
    expect(screen.getByRole("alert")).toHaveTextContent("2 fields need attention");
  });

  it("singularises a lone error", () => {
    render(<FormErrorSummary errors={{ a: { message: "x" } }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("1 field needs attention");
  });

  it("moves focus to the offending field when an entry is pressed", async () => {
    // On a form the size of Console's, an inline error can sit 400px above
    // the submit button — the user presses Save and nothing appears to
    // happen.
    render(
      <>
        <input name="base_url" aria-label="Base URL" />
        <FormErrorSummary errors={errors} />
      </>
    );
    await userEvent.click(screen.getByRole("button", { name: "Include the scheme." }));
    expect(screen.getByLabelText("Base URL")).toHaveFocus();
  });

  it("ignores entries with no message", () => {
    render(<FormErrorSummary errors={{ a: {}, b: { message: "real" } }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("1 field needs attention");
  });
});
