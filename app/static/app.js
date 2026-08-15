// Small, dependency-free helpers. Nothing here talks to anything but this app.

// ---------- Indian number formatting ----------
function inr(value) {
  const number = Math.round(Number(value) || 0);
  const negative = number < 0;
  let digits = String(Math.abs(number));
  if (digits.length > 3) {
    const tail = digits.slice(-3);
    let head = digits.slice(0, -3);
    const groups = [];
    while (head.length > 2) {
      groups.unshift(head.slice(-2));
      head = head.slice(0, -2);
    }
    if (head) groups.unshift(head);
    digits = groups.join(",") + "," + tail;
  }
  return (negative ? "-₹" : "₹") + digits;
}

// ---------- Quick regime comparison on the home page ----------
const quickButton = document.getElementById("q_go");
if (quickButton) {
  quickButton.addEventListener("click", async () => {
    const read = (id) => Number(document.getElementById(id)?.value || 0);
    quickButton.disabled = true;
    quickButton.textContent = "Computing…";
    try {
      const response = await fetch("/api/quick-compare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          salary: read("q_salary"),
          s80c: read("q_80c"),
          s80d: read("q_80d"),
          fd_interest: read("q_interest"),
        }),
      });
      const data = await response.json();
      const winner = data.recommended;
      document.getElementById("q_out").innerHTML = `
        <div class="grid grid-2">
          <div class="stat ${winner === "new" ? "stat-accent" : ""}">
            <div class="stat-label">New regime</div>
            <div class="stat-value">${inr(data.new.total_tax)}</div>
            <div class="stat-note">on total income of ${inr(data.new.total_income)}</div>
          </div>
          <div class="stat ${winner === "old" ? "stat-accent" : ""}">
            <div class="stat-label">Old regime</div>
            <div class="stat-value">${inr(data.old.total_tax)}</div>
            <div class="stat-note">on total income of ${inr(data.old.total_income)}</div>
          </div>
        </div>
        <p class="hint" style="margin-top:.6rem">
          The <strong>${winner}</strong> regime saves ${inr(data.saving)} on these figures.
        </p>`;
    } catch (error) {
      document.getElementById("q_out").innerHTML =
        `<div class="note note-error">Could not compute: ${error}</div>`;
    } finally {
      quickButton.disabled = false;
      quickButton.textContent = "Compare regimes";
    }
  });
}

// ---------- Drag and drop upload ----------
const dropzone = document.querySelector(".dropzone");
if (dropzone) {
  const input = dropzone.querySelector('input[type="file"]');
  const summary = document.getElementById("file-summary");

  const describe = () => {
    if (!summary || !input.files.length) return;
    const names = Array.from(input.files).map((file) => file.name);
    summary.innerHTML = `<strong>${names.length} file(s) ready:</strong> ` +
      names.join(", ");
  };

  ["dragenter", "dragover"].forEach((event) =>
    dropzone.addEventListener(event, (e) => {
      e.preventDefault();
      dropzone.classList.add("is-over");
    })
  );
  ["dragleave", "drop"].forEach((event) =>
    dropzone.addEventListener(event, (e) => {
      e.preventDefault();
      dropzone.classList.remove("is-over");
    })
  );
  dropzone.addEventListener("drop", (e) => {
    input.files = e.dataTransfer.files;
    describe();
  });
  input.addEventListener("change", describe);
}

// ---------- Repeating form rows ----------
document.querySelectorAll("[data-repeat]").forEach((container) => {
  const template = container.querySelector("template");
  const addButton = document.querySelector(
    `[data-repeat-add="${container.dataset.repeat}"]`
  );
  if (!template || !addButton) return;

  const nextIndex = () =>
    container.querySelectorAll(".repeat-row").length;

  addButton.addEventListener("click", () => {
    const index = nextIndex();
    const markup = template.innerHTML.replace(/__INDEX__/g, index);
    const wrapper = document.createElement("div");
    wrapper.innerHTML = markup;
    const row = wrapper.firstElementChild;
    row.querySelector("[data-row-number]").textContent = index + 1;
    container.insertBefore(row, template);
  });

  container.addEventListener("click", (event) => {
    if (!event.target.matches("[data-remove-row]")) return;
    event.target.closest(".repeat-row").remove();
    container.querySelectorAll(".repeat-row").forEach((row, index) => {
      row.querySelector("[data-row-number]").textContent = index + 1;
      row.querySelectorAll("input, select").forEach((field) => {
        if (field.name) {
          field.name = field.name.replace(/_\d+_/, `_${index}_`);
        }
      });
    });
  });
});

// ---------- Select-all on the review screen ----------
const selectAll = document.getElementById("select-all");
if (selectAll) {
  selectAll.addEventListener("change", () => {
    document
      .querySelectorAll('input[name="accept"]')
      .forEach((box) => (box.checked = selectAll.checked));
  });
}
