(function () {
  "use strict";

  var encoder = new TextEncoder();

  function concatenate(parts) {
    var length = parts.reduce(function (total, part) {
      return total + part.length;
    }, 0);
    var output = new Uint8Array(length);
    var offset = 0;
    parts.forEach(function (part) {
      output.set(part, offset);
      offset += part.length;
    });
    return output;
  }

  function uint32(value) {
    var output = new Uint8Array(4);
    new DataView(output.buffer).setUint32(0, value, false);
    return output;
  }

  function sshString(value) {
    var bytes = typeof value === "string" ? encoder.encode(value) : value;
    return concatenate([uint32(bytes.length), bytes]);
  }

  function base64(bytes) {
    var chunks = [];
    for (var offset = 0; offset < bytes.length; offset += 8192) {
      chunks.push(String.fromCharCode.apply(null, bytes.subarray(offset, offset + 8192)));
    }
    return window.btoa(chunks.join(""));
  }

  function readDerElement(bytes, offset) {
    if (offset + 2 > bytes.length) {
      throw new Error("The browser returned an incomplete private key.");
    }
    var tag = bytes[offset];
    var length = bytes[offset + 1];
    var headerLength = 2;
    if (length & 128) {
      var lengthBytes = length & 127;
      if (!lengthBytes || lengthBytes > 4 || offset + 2 + lengthBytes > bytes.length) {
        throw new Error("The browser returned an unsupported private-key encoding.");
      }
      length = 0;
      for (var index = 0; index < lengthBytes; index += 1) {
        length = length * 256 + bytes[offset + 2 + index];
      }
      headerLength += lengthBytes;
    }
    var start = offset + headerLength;
    var end = start + length;
    if (end > bytes.length) {
      throw new Error("The browser returned an incomplete private key.");
    }
    return { tag: tag, start: start, end: end, next: end };
  }

  function ed25519Seed(pkcs8) {
    var outer = readDerElement(pkcs8, 0);
    if (outer.tag !== 48 || outer.end !== pkcs8.length) {
      throw new Error("The browser returned an unsupported private-key encoding.");
    }
    var cursor = outer.start;
    var version = readDerElement(pkcs8, cursor);
    cursor = version.next;
    var algorithm = readDerElement(pkcs8, cursor);
    cursor = algorithm.next;
    var privateKey = readDerElement(pkcs8, cursor);
    if (version.tag !== 2 || algorithm.tag !== 48 || privateKey.tag !== 4) {
      throw new Error("The browser returned an unsupported private-key encoding.");
    }
    var wrapped = pkcs8.subarray(privateKey.start, privateKey.end);
    var seedElement = readDerElement(wrapped, 0);
    if (seedElement.tag !== 4 || seedElement.end - seedElement.start !== 32) {
      throw new Error("The browser returned an unsupported Ed25519 seed.");
    }
    return wrapped.slice(seedElement.start, seedElement.end);
  }

  function openSshPrivateKey(seed, publicKey, comment) {
    var keyType = "ssh-ed25519";
    var publicBlob = concatenate([sshString(keyType), sshString(publicKey)]);
    var check = new Uint8Array(4);
    window.crypto.getRandomValues(check);
    var privateBlock = concatenate([
      check,
      check,
      sshString(keyType),
      sshString(publicKey),
      sshString(concatenate([seed, publicKey])),
      sshString(comment),
    ]);
    var paddingLength = 8 - (privateBlock.length % 8);
    var padding = new Uint8Array(paddingLength);
    for (var index = 0; index < padding.length; index += 1) {
      padding[index] = index + 1;
    }
    privateBlock = concatenate([privateBlock, padding]);
    var envelope = concatenate([
      encoder.encode("openssh-key-v1\0"),
      sshString("none"),
      sshString("none"),
      sshString(new Uint8Array()),
      uint32(1),
      sshString(publicBlob),
      sshString(privateBlock),
    ]);
    var encoded = base64(envelope).match(/.{1,70}/g).join("\n");
    return "-----BEGIN OPENSSH PRIVATE KEY-----\n" + encoded + "\n-----END OPENSSH PRIVATE KEY-----\n";
  }

  function downloadPrivateKey(contents) {
    var date = new Date().toISOString().slice(0, 10).replace(/-/g, "");
    var blob = new Blob([contents], { type: "application/octet-stream" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = "coldfront-pve-" + date + "-ed25519";
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1000);
  }

  function selectedPveResource(resource, pveResourceId) {
    return resource && String(resource.value) === String(pveResourceId);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var resource = document.getElementById("id_resource");
    var resourceData = document.getElementById("pve-provisioner-resource-id");
    var publicKey = document.getElementById("id_vm_ssh_public_key");
    var flavorGroup = document.getElementById("div_id_vm_flavor");
    var publicKeyGroup = document.getElementById("div_id_vm_ssh_public_key");
    if (!resource || !resourceData || !publicKey || !publicKeyGroup) {
      return;
    }
    var pveResourceId = JSON.parse(resourceData.textContent);
    var panel = document.createElement("div");
    panel.id = "pve-ssh-key-generator";
    panel.className = "alert alert-secondary mt-2";
    panel.innerHTML =
      '<p class="mb-2"><strong>Need an SSH key?</strong> Generate an Ed25519 pair locally in this browser. ' +
      "The private key is downloaded once and never submitted to ColdFront.</p>" +
      '<button type="button" class="btn btn-outline-primary btn-sm">Generate and download key pair</button>' +
      '<p class="small mt-2 mb-0" role="status" aria-live="polite">The downloaded private key is unencrypted. ' +
      "Store it securely, run <code>chmod 600</code> on it, and do not upload it here.</p>";
    publicKeyGroup.appendChild(panel);
    var button = panel.querySelector("button");
    var status = panel.querySelector('[role="status"]');

    function updateVisibility() {
      var visible = selectedPveResource(resource, pveResourceId);
      if (flavorGroup) {
        flavorGroup.style.display = visible ? "" : "none";
      }
      publicKeyGroup.style.display = visible ? "" : "none";
    }

    resource.addEventListener("change", updateVisibility);
    updateVisibility();

    button.addEventListener("click", async function () {
      if (publicKey.value.trim()) {
        status.textContent = "Clear the existing public-key field before generating a replacement.";
        publicKey.focus();
        return;
      }
      if (!window.crypto || !window.crypto.subtle) {
        status.textContent = "This browser cannot generate SSH keys. Use ssh-keygen and paste its public key instead.";
        return;
      }
      button.disabled = true;
      status.textContent = "Generating the key pair locally…";
      try {
        var pair = await window.crypto.subtle.generateKey(
          { name: "Ed25519" },
          true,
          ["sign", "verify"]
        );
        var rawPublic = new Uint8Array(
          await window.crypto.subtle.exportKey("raw", pair.publicKey)
        );
        var pkcs8 = new Uint8Array(
          await window.crypto.subtle.exportKey("pkcs8", pair.privateKey)
        );
        var comment = "coldfront-pve";
        var publicBlob = concatenate([sshString("ssh-ed25519"), sshString(rawPublic)]);
        var privateKey = openSshPrivateKey(ed25519Seed(pkcs8), rawPublic, comment);
        downloadPrivateKey(privateKey);
        publicKey.value = "ssh-ed25519 " + base64(publicBlob) + " " + comment;
        publicKey.dispatchEvent(new Event("input", { bubbles: true }));
        publicKey.dispatchEvent(new Event("change", { bubbles: true }));
        status.innerHTML =
          "Private key downloaded. Keep it safe; only the public half shown above will be submitted.";
      } catch (error) {
        status.textContent =
          "Key generation failed in this browser. Use ssh-keygen and paste its public key instead. " +
          (error && error.message ? error.message : "");
      } finally {
        button.disabled = false;
      }
    });
  });
})();
