// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

using System;
using System.IO;
using System.Text;
using Windows.ApplicationModel;
using Windows.Data.Json;
using Windows.Storage;

namespace AtaWakeAgent
{
    internal static class WakeAccess
    {
        private const string FeatureId = "com.microsoft.windows.applicationmodel.conversationalagent_v1";
        internal const string ConfigurationFileName = "wake-access.local.json";
        private const int MaximumConfigurationBytes = 16384;

        internal static bool TryUnlock(out string status)
        {
            string path;
            try
            {
                path = Path.Combine(ApplicationData.Current.LocalFolder.Path, ConfigurationFileName);
            }
            catch
            {
                status = "Hardware wake requires the installed package and its private access configuration. See the wake-agent README.";
                return false;
            }

            if (!TryReadCredentials(path, out var token, out var attestation, out status))
            {
                return false;
            }

            try
            {
                var result = LimitedAccessFeatures.TryUnlockFeature(FeatureId, token, attestation);
                var available = result.Status == LimitedAccessFeatureStatus.Available ||
                    result.Status == LimitedAccessFeatureStatus.AvailableWithoutToken;
                status = $"ConversationalAgent LAF unlock status: {result.Status}.";
                if (!available)
                {
                    status += " Hardware wake stopped. Check Microsoft approval, package identity, and private access configuration.";
                }
                return available;
            }
            catch (Exception exception)
            {
                status = $"ConversationalAgent LAF unlock failed ({exception.GetType().Name}). Hardware wake stopped; see the wake-agent README.";
                return false;
            }
        }

        internal static bool TryReadCredentials(string path, out string token, out string attestation, out string status)
        {
            token = string.Empty;
            attestation = string.Empty;
            status = "Hardware wake access configuration is missing or invalid. Run configure-access.ps1 after installing the package; see the wake-agent README.";
            try
            {
                using (var stream = File.OpenRead(path))
                {
                    if (stream.Length == 0 || stream.Length > MaximumConfigurationBytes)
                    {
                        return false;
                    }
                    using (var reader = new StreamReader(stream, new UTF8Encoding(false, true), true))
                    {
                        if (!JsonObject.TryParse(reader.ReadToEnd(), out var configuration) ||
                            !TryReadValue(configuration, "token", out var configuredToken) ||
                            !TryReadValue(configuration, "attestation", out var configuredAttestation))
                        {
                            return false;
                        }
                        token = configuredToken;
                        attestation = configuredAttestation;
                    }
                }
                status = "Hardware wake access configuration loaded.";
                return true;
            }
            catch
            {
                return false;
            }
        }

        private static bool TryReadValue(JsonObject configuration, string name, out string value)
        {
            value = string.Empty;
            if (!configuration.ContainsKey(name) || configuration[name].ValueType != JsonValueType.String)
            {
                return false;
            }
            value = configuration[name].GetString();
            return !string.IsNullOrWhiteSpace(value) &&
                !value.TrimStart().StartsWith("REPLACE_WITH_", StringComparison.OrdinalIgnoreCase);
        }
    }
}