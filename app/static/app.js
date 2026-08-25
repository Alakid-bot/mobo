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

  const identityCard = document.querySelector("[data-refresh-bot-identity]");
  const identityAvatar = identityCard?.querySelector("[data-bot-avatar]");
  identityAvatar?.addEventListener("error", () => {
    identityAvatar.hidden = true;
    const fallback = identityCard.querySelector("[data-bot-avatar-fallback]");
    if (fallback) fallback.hidden = false;
  });
  identityCard?.addEventListener("click", async () => {
    identityCard.disabled = true;
    try {
      const result = await api("/api/discord/identity/refresh", "POST");
      const identity = result.identity;
      identityCard.querySelector("[data-bot-display-name]").textContent = identity.display_name;
      identityCard.querySelector("[data-bot-tag]").textContent = identity.user_tag;
      if (identity.avatar_available && identityAvatar) {
        identityAvatar.src = `/api/discord/identity/avatar?v=${identity.avatar_version}`;
        identityAvatar.hidden = false;
        const fallback = identityCard.querySelector("[data-bot-avatar-fallback]");
        if (fallback) fallback.hidden = true;
      }
      const guilds = result.guilds;
      const summary = `Bot 身份已刷新；${guilds.synced} 个服务器已清除独立外观，${guilds.unchanged} 个原本已同步`;
      notify(
        guilds.failed ? `${summary}，${guilds.failed} 个服务器因权限不足未完成` : summary,
        guilds.failed ? "error" : "success",
      );
    } catch (error) {
      notify(error.message, "error");
    } finally {
      identityCard.disabled = false;
    }
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

  const channelPolicyForm = document.querySelector("#channel-policy-form");
  const channelGuildSelect = channelPolicyForm?.querySelector("[data-channel-guild]");
  const channelSelect = channelPolicyForm?.querySelector("[data-channel-select]");
  const availableChannels = channelSelect
    ? Array.from(channelSelect.querySelectorAll("option[data-guild-id]")).map((option) => ({
        guildId: option.dataset.guildId,
        channelId: option.value,
        channelName: option.dataset.channelName,
        label: option.textContent,
      }))
    : [];

  function refreshChannelOptions() {
    if (!channelSelect || !channelGuildSelect) return;
    const choices = availableChannels.filter((item) => item.guildId === channelGuildSelect.value);
    channelSelect.replaceChildren(
      new Option(choices.length ? "选择文字频道" : "该服务器没有可用文字频道", ""),
    );
    choices.forEach((item) => {
      const option = new Option(item.label, item.channelId);
      option.dataset.channelName = item.channelName;
      channelSelect.append(option);
    });
    channelSelect.disabled = choices.length === 0;
  }

  channelGuildSelect?.addEventListener("change", refreshChannelOptions);
  refreshChannelOptions();
  channelPolicyForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(channelPolicyForm);
    const selected = channelSelect?.selectedOptions[0];
    const button = event.submitter || channelPolicyForm.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    try {
      await api("/api/channels", "POST", {
        guild_id: form.get("guild_id"),
        channel_id: form.get("channel_id"),
        channel_name: selected?.dataset.channelName || "",
        mode: form.get("mode"),
      });
      notify("频道授权已更新");
      window.setTimeout(() => window.location.reload(), 350);
    } catch (error) {
      notify(error.message, "error");
      if (button) button.disabled = false;
    }
  });

  document.querySelectorAll("[data-delete-channel-setting]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-channel-setting]");
      if (!window.confirm("恢复后，mobo 只会在被艾特或回复时响应。确定继续吗？")) return;
      button.disabled = true;
      try {
        await api(`/api/channels/${row.dataset.guildId}/${row.dataset.channelId}`, "DELETE");
        row.remove();
        notify("频道已恢复为仅直接响应");
      } catch (error) {
        notify(error.message, "error");
        button.disabled = false;
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

  const experienceForm = document.querySelector("#experience-form");
  experienceForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(experienceForm);
    const button = event.submitter;
    if (button) button.disabled = true;
    try {
      await api("/api/experiences", "POST", {
        content: form.get("content"),
        guild_id: form.get("guild_id"),
        importance: form.get("importance"),
        locked: form.get("locked") === "on",
      });
      notify("mobo 的公开经历已保存");
      window.setTimeout(() => window.location.reload(), 350);
    } catch (error) {
      notify(error.message, "error");
    } finally {
      if (button) button.disabled = false;
    }
  });

  document.querySelectorAll("[data-delete-experience]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-experience-id]");
      if (!window.confirm("确定删除这条 mobo 经历吗？")) return;
      button.disabled = true;
      try {
        await api(`/api/experiences/${row.dataset.experienceId}`, "DELETE");
        row.remove();
        notify("mobo 经历已删除");
      } catch (error) {
        notify(error.message, "error");
        button.disabled = false;
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
      const data = new FormData(form);
      const guildId = form.dataset.guildId || data.get("guild_id");
      const button = event.submitter || form.querySelector("button[type=submit]");
      if (button) button.disabled = true;
      try {
        await api(`/api/guilds/${guildId}/persona`, "POST", {
          system_prompt: data.get("system_prompt"),
        });
        notify("服务器人设已保存");
        window.setTimeout(() => window.location.reload(), 350);
      } catch (error) {
        notify(error.message, "error");
        if (button) button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-delete-guild-persona]").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = button.closest("[data-guild-persona]");
      if (!window.confirm("删除后，这个服务器会立即恢复全局核心人设。确定删除吗？")) return;
      button.disabled = true;
      try {
        await api(`/api/guilds/${form.dataset.guildId}/persona`, "DELETE");
        form.remove();
        notify("服务器人设覆盖已删除");
      } catch (error) {
        notify(error.message, "error");
        button.disabled = false;
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

  const discordAdminForm = document.querySelector("#discord-admin-form");
  discordAdminForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(discordAdminForm);
    const button = event.submitter || discordAdminForm.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    try {
      await api("/api/discord-admins", "POST", {
        user_id: form.get("user_id"),
        note: form.get("note"),
      });
      notify("Discord 管理员 ID 已保存");
      window.setTimeout(() => window.location.reload(), 350);
    } catch (error) {
      notify(error.message, "error");
    } finally {
      if (button) button.disabled = false;
    }
  });

  document.querySelectorAll("[data-toggle-discord-admin]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-discord-admin-id]");
      const enabled = button.dataset.enabled !== "true";
      button.disabled = true;
      try {
        await api(`/api/discord-admins/${encodeURIComponent(row.dataset.discordAdminId)}/enabled`, "POST", {
          enabled,
        });
        button.dataset.enabled = String(enabled);
        button.textContent = enabled ? "停用" : "启用";
        const label = row.querySelector(".admin-enabled-label");
        if (label) label.textContent = enabled ? "启用" : "停用";
        notify(enabled ? "管理员 ID 已启用" : "管理员 ID 已停用");
      } catch (error) {
        notify(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-delete-discord-admin]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-discord-admin-id]");
      if (!window.confirm("确定删除这个 Discord 管理员 ID 吗？")) return;
      button.disabled = true;
      try {
        await api(`/api/discord-admins/${encodeURIComponent(row.dataset.discordAdminId)}`, "DELETE");
        row.remove();
        notify("Discord 管理员 ID 已删除");
      } catch (error) {
        notify(error.message, "error");
        button.disabled = false;
      }
    });
  });

  const safetyRuleForm = document.querySelector("#safety-rule-form");
  safetyRuleForm?.querySelector('input[name="enabled"]')?.addEventListener("change", (event) => {
    const label = event.target.closest(".switch")?.querySelector("b");
    if (label) label.textContent = event.target.checked ? "已开启" : "已关闭";
  });
  safetyRuleForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(safetyRuleForm);
    const ruleId = String(form.get("id") || "").trim();
    const button = event.submitter || safetyRuleForm.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    const payload = {
      name: form.get("name"),
      category: form.get("category"),
      direction: form.get("direction"),
      pattern: form.get("pattern"),
      match_type: form.get("match_type"),
      action: form.get("action"),
      replacement: form.get("replacement"),
      priority: form.get("priority"),
      enabled: form.get("enabled") === "on",
    };
    try {
      await api(ruleId ? `/api/safety-rules/${encodeURIComponent(ruleId)}` : "/api/safety-rules", ruleId ? "PUT" : "POST", payload);
      notify(ruleId ? "安全规则已更新" : "安全规则已添加");
      window.setTimeout(() => window.location.reload(), 350);
    } catch (error) {
      notify(error.message, "error");
    } finally {
      if (button) button.disabled = false;
    }
  });

  document.querySelector("[data-reset-safety-rule]")?.addEventListener("click", () => {
    if (!safetyRuleForm) return;
    safetyRuleForm.reset();
    safetyRuleForm.querySelector('input[name="id"]').value = "";
    const state = safetyRuleForm.querySelector("[data-safety-form-state]");
    if (state) state.textContent = "新增规则";
    const enabled = safetyRuleForm.querySelector('input[name="enabled"]');
    if (enabled) {
      enabled.checked = true;
      const label = enabled.closest(".switch")?.querySelector("b");
      if (label) label.textContent = "已开启";
    }
  });

  document.querySelectorAll("[data-edit-safety-rule]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!safetyRuleForm) return;
      const row = button.closest("[data-safety-rule-id]");
      const values = {
        id: row.dataset.safetyRuleId,
        name: row.dataset.ruleName,
        category: row.dataset.ruleCategory,
        direction: row.dataset.ruleDirection,
        pattern: row.dataset.rulePattern,
        match_type: row.dataset.ruleMatchType,
        action: row.dataset.ruleAction,
        replacement: row.dataset.ruleReplacement,
        priority: row.dataset.rulePriority,
      };
      Object.entries(values).forEach(([key, value]) => {
        const input = safetyRuleForm.querySelector(`[name="${key}"]`);
        if (input) input.value = value;
      });
      const enabled = safetyRuleForm.querySelector('input[name="enabled"]');
      if (enabled) {
        enabled.checked = row.dataset.ruleEnabled === "true";
        const label = enabled.closest(".switch")?.querySelector("b");
        if (label) label.textContent = enabled.checked ? "已开启" : "已关闭";
      }
      const state = safetyRuleForm.querySelector("[data-safety-form-state]");
      if (state) state.textContent = `正在编辑规则 #${row.dataset.safetyRuleId}`;
      safetyRuleForm.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  document.querySelectorAll("[data-toggle-safety-rule]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-safety-rule-id]");
      const enabled = button.dataset.enabled !== "true";
      button.disabled = true;
      try {
        await api(`/api/safety-rules/${encodeURIComponent(row.dataset.safetyRuleId)}/enabled`, "POST", { enabled });
        button.dataset.enabled = String(enabled);
        button.textContent = enabled ? "停用" : "启用";
        row.dataset.ruleEnabled = String(enabled);
        const label = row.querySelector("b");
        if (label) label.textContent = enabled ? "启用" : "停用";
        notify(enabled ? "安全规则已启用" : "安全规则已停用");
      } catch (error) {
        notify(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-delete-safety-rule]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-safety-rule-id]");
      if (!window.confirm("确定删除这条安全规则吗？")) return;
      button.disabled = true;
      try {
        await api(`/api/safety-rules/${encodeURIComponent(row.dataset.safetyRuleId)}`, "DELETE");
        row.remove();
        notify("安全规则已删除");
      } catch (error) {
        notify(error.message, "error");
        button.disabled = false;
      }
    });
  });

  const modelForm = document.querySelector("#model-center-form");
  if (modelForm) {
    const baseUrl = modelForm.querySelector('[name="base_url"]');
    const apiKey = modelForm.querySelector('[name="api_key"]');
    const clearApiKey = modelForm.querySelector('[name="clear_api_key"]');
    const role = modelForm.querySelector('[name="role"]');
    const model = modelForm.querySelector('[name="model"]');
    const options = modelForm.querySelector("#model-options");
    const count = modelForm.querySelector("[data-model-count]");
    const state = document.querySelector("[data-model-state]");
    const payload = () => ({
      base_url: baseUrl.value.trim(),
      api_key: apiKey.value.trim(),
      clear_api_key: clearApiKey.checked,
      role: role.value,
      model: model.value.trim(),
    });
    const markUntested = () => {
      if (state) state.textContent = "尚未测试";
    };
    role.addEventListener("change", () => {
      const key = `${role.value}Model`;
      model.value = modelForm.dataset[key] || modelForm.dataset.chatModel || "";
      markUntested();
    });
    [baseUrl, apiKey, clearApiKey].forEach((input) => input.addEventListener("input", () => {
      if (options) options.replaceChildren();
      if (count) count.textContent = "连接信息已更改，请重新拉取模型";
      markUntested();
    }));
    model.addEventListener("input", markUntested);

    modelForm.querySelector("[data-model-discover]")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await api("/api/models/discover", "POST", { ...payload(), force: true });
        if (options) {
          options.replaceChildren(...result.models.map((name) => {
            const option = document.createElement("option");
            option.value = name;
            return option;
          }));
        }
        if (count) count.textContent = `已拉取 ${result.count} 个模型，可输入关键词筛选`;
        notify("连接成功，模型列表已更新；测试并启用后才会保存连接");
      } catch (error) {
        notify(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });

    modelForm.querySelector("[data-model-test]")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await api("/api/models/test", "POST", payload());
        if (state) state.textContent = `连接正常 · ${result.latency_ms} ms`;
        notify(`模型连接正常，耗时 ${result.latency_ms} ms`);
      } catch (error) {
        if (state) state.textContent = "测试失败";
        notify(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });

    modelForm.querySelector("[data-model-activate]")?.addEventListener("click", async (event) => {
      if (!window.confirm("将先进行真实测试；只有成功后才启用这个模型。继续吗？")) return;
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await api("/api/models/activate", "POST", payload());
        modelForm.dataset[`${result.role}Model`] = result.model;
        modelForm.dataset.keyConfigured = String(result.key_configured);
        apiKey.value = "";
        apiKey.placeholder = result.key_configured ? "已配置 · 留空继续使用" : "无鉴权接口可留空";
        const badge = apiKey.closest(".secret-input")?.querySelector("span");
        if (badge) badge.textContent = result.key_configured ? "SET" : "EMPTY";
        clearApiKey.checked = false;
        if (state) state.textContent = "已测试并启用";
        notify(`${result.model} 与接口连接已安全保存并启用`);
      } catch (error) {
        if (state) state.textContent = "未启用 · 测试失败";
        notify(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  }

  const modelSettingsForm = document.querySelector("#model-settings-form");
  modelSettingsForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = {};
    new FormData(modelSettingsForm).forEach((value, key) => {
      values[key] = value;
    });
    const button = event.submitter || modelSettingsForm.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    try {
      await api("/api/models/settings", "POST", { values });
      notify("生成参数已保存并立即生效");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      if (button) button.disabled = false;
    }
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
