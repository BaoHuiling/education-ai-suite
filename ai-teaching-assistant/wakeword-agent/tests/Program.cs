// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

using System;
using System.IO;
using System.Text;
using AtaWakeAgent;

internal static class Program
{
    private static int Main()
    {
        var directory = Path.Combine(Path.GetTempPath(), "ata-wake-access-tests-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        var path = Path.Combine(directory, WakeAccess.ConfigurationFileName);
        try
        {
            Check("missing file", path, false);
            var cases = new[]
            {
                ("valid configuration", "{\"token\":\"test-token\",\"attestation\":\"test-attestation\"}", true),
                ("empty file", "", false),
                ("missing attestation", "{\"token\":\"test-token\"}", false),
                ("empty token", "{\"token\":\" \",\"attestation\":\"test-attestation\"}", false),
                ("wrong JSON type", "{\"token\":123,\"attestation\":\"test-attestation\"}", false),
                ("token placeholder", "{\"token\":\"REPLACE_WITH_MICROSOFT_ISSUED_TOKEN\",\"attestation\":\"test-attestation\"}", false),
                ("attestation placeholder", "{\"token\":\"test-token\",\"attestation\":\"REPLACE_WITH_MICROSOFT_APPROVED_ATTESTATION\"}", false),
                ("malformed JSON", "{\"token\":\"test-token\", BROKEN test-attestation", false),
                ("JSON array", "[]", false),
                ("oversized file", new string(' ', 16385), false)
            };
            foreach (var testCase in cases)
            {
                File.WriteAllText(path, testCase.Item2, new UTF8Encoding(false));
                Check(testCase.Item1, path, testCase.Item3);
            }
            File.WriteAllBytes(path, new byte[] { 0xff, 0xff, 0xff });
            Check("invalid encoding", path, false);
            if (WakeAccess.TryUnlock(out var status) || string.IsNullOrWhiteSpace(status))
            {
                throw new InvalidOperationException("Unpackaged execution must reject hardware activation.");
            }
            Console.WriteLine("PASS: unpackaged activation rejected");
            return 0;
        }
        finally
        {
            Directory.Delete(directory, true);
        }
    }

    private static void Check(string name, string path, bool expected)
    {
        var result = WakeAccess.TryReadCredentials(path, out var token, out var attestation, out var status);
        if (result != expected || string.IsNullOrWhiteSpace(status) ||
            status.Contains("test-token") || status.Contains("test-attestation") ||
            (!result && (token.Length != 0 || attestation.Length != 0)))
        {
            throw new InvalidOperationException("Configuration test failed: " + name);
        }
        if (result && (token != "test-token" || attestation != "test-attestation"))
        {
            throw new InvalidOperationException("Configuration values were altered: " + name);
        }
        Console.WriteLine("PASS: " + name);
    }
}