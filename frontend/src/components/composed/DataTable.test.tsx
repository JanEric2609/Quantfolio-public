import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DataTable } from "./DataTable";
import type { ColumnDef } from "@tanstack/react-table";

interface Row { name: string; value: number }
const cols: ColumnDef<Row>[] = [
  { accessorKey: "name",  header: "Name" },
  { accessorKey: "value", header: "Value" },
];
const rows: Row[] = [
  { name: "Bravo", value: 2 },
  { name: "Alpha", value: 1 },
  { name: "Charlie", value: 3 },
];

describe("DataTable", () => {
  it("renders all rows by default", () => {
    render(<DataTable data={rows} columns={cols} enableFilter />);
    expect(screen.getAllByRole("row")).toHaveLength(rows.length + 1); // +1 header
  });
  it("sorts when a header is clicked", async () => {
    render(<DataTable data={rows} columns={cols} />);
    await userEvent.click(screen.getByRole("button", { name: /Name/i }));
    const bodyRows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(bodyRows[0]).toHaveTextContent("Alpha");
  });
  it("filters by text", async () => {
    render(<DataTable data={rows} columns={cols} enableFilter />);
    await userEvent.type(screen.getByPlaceholderText(/filter/i), "Alp");
    const bodyRows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(bodyRows).toHaveLength(1);
    expect(bodyRows[0]).toHaveTextContent("Alpha");
  });
  it("shows empty state when data is empty", () => {
    render(<DataTable data={[]} columns={cols} emptyState={<span>nothing</span>} />);
    expect(screen.getByText("nothing")).toBeInTheDocument();
  });
});

describe("DataTable keyboard access", () => {
  const bodyRows = () => within(screen.getByRole("table")).getAllByRole("row").slice(1);

  it("makes rows focusable and activates them with Enter and Space when onRowClick is set", async () => {
    const user = userEvent.setup();
    const onRowClick = vi.fn();
    render(<DataTable data={rows} columns={cols} onRowClick={onRowClick} />);

    const first = bodyRows()[0];
    expect(first).toHaveAttribute("tabindex", "0");

    await user.tab(); // header sort button "Name"
    await user.tab(); // header sort button "Value"
    await user.tab(); // first body row
    expect(first).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(onRowClick).toHaveBeenLastCalledWith(rows[0]);

    await user.keyboard(" ");
    expect(onRowClick).toHaveBeenCalledTimes(2);

    await user.tab();
    expect(bodyRows()[1]).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onRowClick).toHaveBeenLastCalledWith(rows[1]);
  });

  it("still handles mouse clicks on rows", async () => {
    const onRowClick = vi.fn();
    render(<DataTable data={rows} columns={cols} onRowClick={onRowClick} />);
    await userEvent.click(screen.getByText("Charlie"));
    expect(onRowClick).toHaveBeenCalledWith(rows[2]);
  });

  it("leaves rows out of the tab order when nothing is clickable", () => {
    render(<DataTable data={rows} columns={cols} />);
    for (const row of bodyRows()) expect(row).not.toHaveAttribute("tabindex");
  });

  it("does not hijack Enter pressed on a control inside a row", async () => {
    const user = userEvent.setup();
    const onRowClick = vi.fn();
    const inner = vi.fn();
    const withButton: ColumnDef<Row>[] = [
      ...cols,
      { id: "act", header: "Act", cell: () => <button type="button" onClick={(e) => { e.stopPropagation(); inner(); }}>Go</button> },
    ];
    render(<DataTable data={rows} columns={withButton} onRowClick={onRowClick} />);
    screen.getAllByRole("button", { name: "Go" })[0].focus();
    await user.keyboard("{Enter}");
    expect(inner).toHaveBeenCalledTimes(1);
    expect(onRowClick).not.toHaveBeenCalled();
  });
});

describe("DataTable phone card layout", () => {
  const realMatchMedia = window.matchMedia;
  afterEach(() => {
    window.matchMedia = realMatchMedia;
  });

  function setPhone(isPhone: boolean) {
    window.matchMedia = ((query: string) => ({
      matches: isPhone && query.includes("max-width"),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia;
  }

  const card = (row: Row) => <span>{row.name} costs {row.value}</span>;

  it("keeps the table on desktop even when a card renderer is supplied", () => {
    setPhone(false);
    render(<DataTable data={rows} columns={cols} mobileCard={card} />);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.queryByText("Bravo costs 2")).toBeNull();
  });

  it("renders one card per row instead of a table below sm", () => {
    setPhone(true);
    render(<DataTable data={rows} columns={cols} mobileCard={card} />);
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByText("Alpha costs 1")).toBeInTheDocument();
  });

  it("makes each card a button that opens the row when onRowClick is set", async () => {
    setPhone(true);
    const onRowClick = vi.fn();
    render(<DataTable data={rows} columns={cols} mobileCard={card} onRowClick={onRowClick} />);
    await userEvent.click(screen.getByRole("button", { name: "Charlie costs 3" }));
    expect(onRowClick).toHaveBeenCalledWith(rows[2]);
  });

  it("keeps filtering and offers a sort control in card mode", async () => {
    setPhone(true);
    render(<DataTable data={rows} columns={cols} mobileCard={card} enableFilter />);

    await userEvent.type(screen.getByPlaceholderText(/filter/i), "Brav");
    expect(screen.getAllByRole("listitem")).toHaveLength(1);

    await userEvent.clear(screen.getByPlaceholderText(/filter/i));
    await userEvent.selectOptions(screen.getByLabelText("Sort by"), "value");
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Alpha costs 1");
    expect(items[2]).toHaveTextContent("Charlie costs 3");

    await userEvent.click(screen.getByRole("button", { name: /switch to descending/i }));
    expect(screen.getAllByRole("listitem")[0]).toHaveTextContent("Charlie costs 3");
  });

  it("shows the empty state in card mode", () => {
    setPhone(true);
    render(<DataTable data={[]} columns={cols} mobileCard={card} emptyState={<span>nothing</span>} />);
    expect(screen.getByText("nothing")).toBeInTheDocument();
  });
});
