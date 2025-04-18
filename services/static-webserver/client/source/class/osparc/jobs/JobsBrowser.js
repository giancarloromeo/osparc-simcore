/* ************************************************************************

   osparc - the simcore frontend

   https://osparc.io

   Copyright:
     2025 IT'IS Foundation, https://itis.swiss

   License:
     MIT: https://opensource.org/licenses/MIT

   Authors:
     * Odei Maiz (odeimaiz)

************************************************************************ */


qx.Class.define("osparc.jobs.JobsBrowser", {
  extend: qx.ui.core.Widget,

  construct() {
    this.base(arguments);

    this._setLayout(new qx.ui.layout.VBox(10));

    const jobsFilter = this.getChildControl("jobs-filter");
    this.getChildControl("jobs-ongoing");
    const jobsTable = this.getChildControl("jobs-table");

    jobsFilter.getChildControl("textfield").addListener("input", e => {
      const filterText = e.getData();
      jobsTable.getTableModel().setFilters({
        text: filterText,
      });
    });
  },

  members: {
    _createChildControlImpl: function(id) {
      let control;
      switch (id) {
        case "header-filter":
          control = new qx.ui.container.Composite(new qx.ui.layout.HBox(5));
          this._add(control);
          break;
        case "jobs-filter":
          control = new osparc.filter.TextFilter("text", "jobsList").set({
            allowStretchX: true,
            margin: 0
          });
          this.getChildControl("header-filter").add(control, {
            flex: 1
          });
          break;
        case "jobs-ongoing":
          control = new qx.ui.form.CheckBox().set({
            label: "Hide finished jobs",
            value: true,
            enabled: false,
          });
          this.getChildControl("header-filter").add(control);
          break;
        case "jobs-table":
          control = new osparc.jobs.JobsTable();
          this._add(control);
          break;
      }

      return control || this.base(arguments, id);
    },
  }
})
