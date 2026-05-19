const { SANDBOX_MODE } = require("../config");
const { createDockerExecutor } = require("./dockerExecutor");
const { createHostExecutor } = require("./hostExecutor");

function createExecutor(template) {
  if (SANDBOX_MODE === "host") {
    return createHostExecutor(template);
  }
  return createDockerExecutor(template);
}

module.exports = { createExecutor };
