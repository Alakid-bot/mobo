(() => {
  "use strict";

  const csrf = document.body?.dataset.csrf || "";
  const toast = document.querySelector("[data-toast]");

  function notify(message, kind = "success") {
    if (!toast) return;
    toast.textContent = message;
    toast.className = `toast show ${kind}`;
    window.clearTimeout(notify.timer);
    notify.timer = window.setTimeout(() => {
      toast.className = "toast";
    }, 3600);
  }

  async function api(url, method = "POST", payload = undefined) {
    const options = {
      method,
      headers: { "X-CSRF-Token": csrf },
      credentials: "same-origin",
    };
    if (payload !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(url, options);
    let data;
    try {
      data = await response.json();
    } catch (_) {
      data = { ok: false, error: "服务器返回了无法解析的响应" };
    }
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("登录已过期");
    }
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `请求失败（${response.status}）`);
    }
    return data;
  }

  document.querySelector("[data-menu-toggle]")?.addEventListener("click", () => {
    document.querySelector("#sidebar")?.classList.toggle("open");
  });

  const settingsForm = document.querySelector("#settings-form");
  if (settingsForm) {
    const dirtyState = document.querySelector("[data-dirty-state]");
    settingsForm.addEventListener("input", (event) => {
      if (dirtyState) dirtyState.textContent = "有尚未保存的更改";
      const switchRoot = event.target.closest?.(".switch");
      if (switchRoot && event.target.type === "checkbox") {
        switchRoot.querySelector("b").textContent = event.target.checked ? "已开启" : "已关闭";
      }
    });
    settingsForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const values = {};
      settingsForm.querySelectorAll("[data-setting]").forEach((input) => {
        const key = input.dataset.key;
        const kind = input.dataset.kind;
        if (kind === "toggle") values[key] = input.checked;
        else if (kind === "number") values[key] = input.value;
        else if (kind === "secret") {
          if (input.value) values[key] = input.value;
        } else values[key] = input.value;
      });
      const clearSecrets = Array.from(
        settingsForm.querySelectorAll("[data-clear-secret]:checked")
      ).map((input) => input.dataset.clearSecret);
      const button = event.submitter;
      if (button) button.disabled = true;
      try {
        const result = await api("/api/settings", "POST", {
          values,
          clear_secrets: clearSecrets,
        });
        settingsForm.querySelectorAll('[data-kind="secret"]').forEach((input) => {
          const configured = Boolean(result.values[input.dataset.key]);
          input.placeholder = configured ? "已配置 · 输入新值才会替换" : "尚未配置";
          const badge = input.closest(".secret-input")?.querySelector("span");
          if (badge) badge.textContent = configured ? "SET" : "EMPTY";
          input.value = "";
        });
        settingsForm.querySelectorAll("[data-clear-secret]").forEach((input) => {
          input.checked = false;
        });
        if (dirtyState) dirtyState.textContent = "全部更改已保存";
        notify("配置已保存并立即生效");
      } catch (error) {
        notify(error.message, "error");
      } finally {
        if (button) button.disabled = false;
      }
    });
  }

  document.querySelectorAll("[data-save-channel]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-channel-row]");
      const listen = row.querySelector("[data-channel-listen]").checked;
      const proactive = row.querySelector("[data-channel-proactive]").checked;
      button.disabled = true;
      try {
        await api("/api/channels", "POST", {
          guild_id: row.dataset.guildId,
          channel_id: row.dataset.channelId,
          channel_name: row.dataset.channelName,
          listen_enabled: listen,
          proactive_enabled: proactive,
        });
        notify(`#${row.dataset.channelName} 的策略已保存`);
      } catch (error) {
        notify(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-channel-proactive]").forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) {
        input.closest("[data-channel-row]").querySelector("[data-channel-listen]").checked = true;
      }
    });
  });

  const preferenceForm = document.querySelector("#preference-form");
  preferenceForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(preferenceForm);
    try {
      await api("/api/preferences", "POST", {
        topic: form.get("topic"),
        keywords: form.get("keywords"),
        weight: form.get("weight"),
        locked: form.get("locked") === "on",
      });
      notify("主题偏好已保存");
      window.setTimeout(() => window.location.reload(), 500);
    } catch (error) {
      notify(error.message, "error");
    }
  });

  document.querySelectorAll("[data-delete-preference]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-preference-id]");
      if (!window.confirm("确定删除这个偏好主题吗？")) return;
      try {
        await api(`/api/preferences/${row.dataset.preferenceId}`, "DELETE");
        row.remove();
        notify("偏好主题已删除");
      } catch (error) {
        notify(error.message, "error");
      }
    });
  });

  const moodForm = document.querySelector("#mood-form");
  moodForm?.querySelectorAll('input[type="range"]').forEach((input) => {
    input.addEventListener("input", () => {
      const output = moodForm.querySelector(`[data-output="${input.name}"]`);
      if (output) output.value = Number(input.value).toFixed(2);
    });
  });
  moodForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(moodForm);
    try {
      await api("/api/mood", "POST", {
        valence: form.get("valence"),
        energy: form.get("energy"),
        social_budget: form.get("social_budget"),
      });
      notify("临时情绪已更新");
    } catch (error) {
      notify(error.message, "error");
    }
  });

  document.querySelectorAll("[data-guild-persona]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await api(`/api/guilds/${form.dataset.guildId}/persona`, "POST", {
          system_prompt: new FormData(form).get("system_prompt"),
        });
        notify("服务器人设已保存");
      } catch (error) {
        notify(error.message, "error");
      }
    });
  });

  document.querySelectorAll("[data-delete-memory]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-memory-id]");
      if (!window.confirm("确定删除这条长期记忆吗？用户之后也无法再看到它。")) return;
      try {
        await api(`/api/memories/${row.dataset.memoryId}`, "DELETE");
        row.remove();
        notify("记忆已删除");
      } catch (error) {
        notify(error.message, "error");
      }
    });
  });

  const passwordForm = document.querySelector("#password-form");
  passwordForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(passwordForm);
    try {
      const result = await api("/api/password", "POST", {
        current_password: form.get("current_password"),
        new_password: form.get("new_password"),
      });
      if (result.reauthenticate) {
        notify("密码已更换，请重新登录");
        window.setTimeout(() => window.location.assign("/login"), 800);
      }
    } catch (error) {
      notify(error.message, "error");
    }
  });
})();
