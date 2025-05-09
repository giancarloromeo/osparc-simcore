/* ************************************************************************

   osparc - the simcore frontend

   https://osparc.io

   Copyright:
     2023 IT'IS Foundation, https://itis.swiss

   License:
     MIT: https://opensource.org/licenses/MIT

   Authors:
     * Odei Maiz (odeimaiz)

************************************************************************ */


qx.Class.define("osparc.info.Conversations", {
  extend: qx.ui.core.Widget,

  /**
    * @param studyData {String} Study Data
    */
  construct: function(studyData) {
    this.base(arguments);

    this.__studyData = studyData;

    this._setLayout(new qx.ui.layout.VBox(10));

    this.__buildLayout();

    this.fetchComments();
  },

  statics: {
    popUpInWindow: function(studyData) {
      const conversations = new osparc.info.Conversations(studyData);
      const title = qx.locale.Manager.tr("Conversations");
      const viewWidth = 600;
      const viewHeight = 700;
      const win = osparc.ui.window.Window.popUpInWindow(conversations, title, viewWidth, viewHeight);
      return win;
    },
  },

  members: {
    __studyData: null,
    __nextRequestParams: null,

    _createChildControlImpl: function(id) {
      let control;
      switch (id) {
        case "title":
          control = new qx.ui.basic.Label().set({
            value: this.tr("0 Comments")
          });
          this._add(control);
          break;
        case "comments-list":
          control = new qx.ui.container.Composite(new qx.ui.layout.VBox(5)).set({
            alignY: "middle"
          });
          this._add(control, {
            flex: 1
          });
          break;
        case "load-more-button":
          control = new osparc.ui.form.FetchButton(this.tr("Load more comments..."));
          control.addListener("execute", () => this.fetchComments(false));
          this._add(control);
          break;
        case "add-comment":
          if (osparc.data.model.Study.canIWrite(this.__studyData["accessRights"])) {
            control = new osparc.info.CommentAdd(this.__studyData["uuid"]);
            control.setPaddingLeft(10);
            control.addListener("commentAdded", () => this.fetchComments());
            this._add(control);
          }
          break;
      }

      return control || this.base(arguments, id);
    },

    __buildLayout: function() {
      this.getChildControl("title");
      this.getChildControl("comments-list");
      this.getChildControl("load-more-button");
      this.getChildControl("add-comment");
    },

    fetchComments: function(removeComments = true) {
      const loadMoreButton = this.getChildControl("load-more-button");
      loadMoreButton.show();
      loadMoreButton.setFetching(true);

      if (removeComments) {
        this.getChildControl("comments-list").removeAll();
      }

      this.__getNextRequest()
        .then(resp => {
          const comments = resp["data"];
          this.__addComments(comments);
          this.__nextRequestParams = resp["_links"]["next"];
          if (this.__nextRequestParams === null) {
            loadMoreButton.exclude();
          }
        })
        .finally(() => loadMoreButton.setFetching(false));
    },

    __getNextRequest: function() {
      const params = {
        url: {
          studyId: this.__studyData["uuid"],
          offset: 0,
          limit: 20
        }
      };
      const nextRequestParams = this.__nextRequestParams;
      if (nextRequestParams) {
        params.url.offset = nextRequestParams.offset;
        params.url.limit = nextRequestParams.limit;
      }
      const options = {
        resolveWResponse: true
      };
      return osparc.data.Resources.fetch("studyComments", "getPage", params, options);
    },

    __addComments: function(comments) {
      const commentsTitle = this.getChildControl("title");
      if (comments.length === 1) {
        commentsTitle.setValue(this.tr("1 Comment"));
      } else if (comments.length > 1) {
        commentsTitle.setValue(comments.length + this.tr(" Comments"));
      }

      const commentsList = this.getChildControl("comments-list");
      comments.forEach(comment => {
        const commentUi = new osparc.info.CommentUI(comment);
        commentsList.add(commentUi);
      });
    }
  }
});
