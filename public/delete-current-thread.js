(() => {
  const nativeFetch = window.fetch.bind(window);

  window.fetch = async (input, init) => {
    const response = await nativeFetch(input, init);
    const method = (init?.method || input?.method || "GET").toUpperCase();
    const requestUrl = new URL(input?.url || input, window.location.href);

    if (
      response.ok &&
      method === "DELETE" &&
      requestUrl.pathname.endsWith("/project/thread") &&
      typeof init?.body === "string"
    ) {
      let threadId;
      try {
        threadId = JSON.parse(init.body).threadId;
      } catch {
        return response;
      }

      const threadPath = `/thread/${encodeURIComponent(threadId)}`;
      const currentPath = window.location.pathname.replace(/\/$/, "");

      if (currentPath.endsWith(threadPath)) {
        window.location.replace(`${currentPath.slice(0, -threadPath.length)}/`);
      }
    }

    return response;
  };
})();
