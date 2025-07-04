// node 3D_Anatomical.js [url_prefix] [template_uuid] [timeout] [--demo]

const tutorialBase = require('../tutorials/tutorialBase');
const utils = require('../utils/utils');

const args = process.argv.slice(2);
const {
  urlPrefix,
  templateUuid,
  startTimeout,
  basicauthUsername,
  basicauthPassword,
  enableDemoMode
} = utils.parseCommandLineArgumentsAnonymous(args);

const anonURL = urlPrefix + templateUuid;
const screenshotPrefix = "3DAnatomical_";


async function runTutorial () {
  const tutorial = new tutorialBase.TutorialBase(anonURL, screenshotPrefix, null, null, null, basicauthUsername, basicauthPassword, enableDemoMode);

  try {
    const page = await tutorial.beforeScript();
    const studyData = await tutorial.openStudyLink();

    const workbenchData = utils.extractWorkbenchData(studyData["data"]);
    const vtkNodeId = workbenchData["nodeIds"][1];
    await tutorial.waitForServices(
      workbenchData["studyId"],
      [vtkNodeId],
      startTimeout,
      false
    );

    await tutorial.waitFor(10000, 'Some time for starting the service');
    await utils.takeScreenshot(page, screenshotPrefix + 'service_started');

    const iframe = await tutorial.getIframe(vtkNodeId);
    const entitiesListed = [
      "Vein.vtk",
      "Artery.vtk",
      "Bones.e",
    ];
    for (const text of entitiesListed) {
      const found = await utils.waitForLabelText(iframe, text);
      if (!found) {
        throw new Error(`Text "${text}" not visible on the page within timeout.`);
      }
    }
  }
  catch(err) {
    await tutorial.setTutorialFailed();
    console.log('Tutorial error: ' + err);
  }
  finally {
    await tutorial.logOut();
    await tutorial.close();
  }

  if (tutorial.getTutorialFailed()) {
    throw "Tutorial Failed";
  }
}

runTutorial()
  .catch(error => {
    console.log('Puppeteer error: ' + error);
    process.exit(1);
  });
