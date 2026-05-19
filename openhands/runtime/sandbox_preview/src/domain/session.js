function createSession({ id, executor, template }) {
  return {
    id,
    executor,
    template,
    status: "creating",
    createdAt: Date.now(),
  };
}

module.exports = { createSession };
