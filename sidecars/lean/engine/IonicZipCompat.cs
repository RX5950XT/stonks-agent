// Stonks Agent clean-room compatibility layer. Copyright 2026 contributors.
// Licensed under Apache-2.0. Replaces vulnerable DotNetZip at build time.

using System;
using System.Collections;
using System.Collections.Generic;
using System.IO.Compression;
using System.IO;
using System.Linq;

namespace Ionic.Zlib
{
    public class ZlibException : IOException
    {
        public ZlibException(string message)
            : base(message)
        {
        }
    }

    public enum Zip64Option
    {
        Default,
        Never,
        AsNecessary,
        Always,
    }
}

namespace Ionic.Zip
{
    public class ZipException : IOException
    {
        public ZipException(string message)
            : base(message)
        {
        }

        public ZipException(string message, Exception innerException)
            : base(message, innerException)
        {
        }
    }

    public sealed class ZipEntry
    {
        private readonly byte[] _content;

        internal ZipEntry(string fileName, byte[] content)
        {
            FileName = ValidateEntryName(fileName);
            _content = content;
        }

        public string FileName { get; }

        public long UncompressedSize => _content.LongLength;

        public Stream OpenReader() =>
            new MemoryStream(_content, writable: false);

        public void Extract(Stream target)
        {
            ArgumentNullException.ThrowIfNull(target);
            target.Write(_content, 0, _content.Length);
        }

        internal ReadOnlyMemory<byte> Content => _content;

        internal static string ValidateEntryName(string value)
        {
            ArgumentException.ThrowIfNullOrWhiteSpace(value);
            var normalized = value.Replace('\\', '/');
            if (Path.IsPathRooted(normalized)
                || normalized.Split('/').Any(part => part is "" or "." or ".."))
            {
                throw new InvalidDataException("Unsafe zip entry name");
            }
            return normalized;
        }
    }

    public sealed class ZipFile : IEnumerable<ZipEntry>, IDisposable
    {
        private const long MaximumEntryBytes = 64L * 1024 * 1024;
        private const long MaximumArchiveBytes = 512L * 1024 * 1024;
        private readonly List<ZipEntry> _entries;
        private bool _disposed;

        public ZipFile(string fileName)
            : this(Load(File.OpenRead(fileName)))
        {
        }

        private ZipFile(List<ZipEntry> entries)
        {
            _entries = entries;
        }

        public int Count => Entries.Count;

        public IReadOnlyList<ZipEntry> Entries
        {
            get
            {
                ThrowIfDisposed();
                return _entries;
            }
        }

        public IReadOnlyList<string> EntryFileNames =>
            Entries.Select(entry => entry.FileName).ToArray();

        public Ionic.Zlib.Zip64Option UseZip64WhenSaving { get; set; }

        public ZipEntry this[int index] => Entries[index];

        public ZipEntry this[string name] =>
            Entries.First(entry =>
                string.Equals(entry.FileName, name, StringComparison.OrdinalIgnoreCase));

        public static ZipFile Read(string fileName) =>
            new(Load(File.OpenRead(fileName)));

        public static ZipFile Read(Stream source)
        {
            ArgumentNullException.ThrowIfNull(source);
            return new ZipFile(Load(source, leaveOpen: true));
        }

        public bool ContainsEntry(string name) =>
            Entries.Any(entry =>
                string.Equals(entry.FileName, name, StringComparison.OrdinalIgnoreCase));

        public ZipEntry AddEntry(string name, byte[] content)
        {
            ThrowIfDisposed();
            ArgumentNullException.ThrowIfNull(content);
            if (content.LongLength > MaximumEntryBytes)
            {
                throw new InvalidDataException("Zip entry exceeds safe limit");
            }
            var entry = new ZipEntry(name, content.ToArray());
            _entries.Add(entry);
            return entry;
        }

        public void RemoveEntry(string name)
        {
            ThrowIfDisposed();
            _entries.RemoveAll(entry =>
                string.Equals(entry.FileName, name, StringComparison.OrdinalIgnoreCase));
        }

        public void Save(string fileName)
        {
            ThrowIfDisposed();
            using var stream = new FileStream(
                fileName,
                FileMode.Create,
                FileAccess.Write,
                FileShare.None);
            using var archive = new ZipArchive(stream, ZipArchiveMode.Create);
            foreach (var entry in _entries)
            {
                var target = archive.CreateEntry(
                    entry.FileName,
                    System.IO.Compression.CompressionLevel.Optimal);
                using var writer = target.Open();
                writer.Write(entry.Content.Span);
            }
        }

        public IEnumerator<ZipEntry> GetEnumerator() =>
            Entries.GetEnumerator();

        IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();

        public void Dispose()
        {
            _disposed = true;
        }

        private static List<ZipEntry> Load(Stream source, bool leaveOpen = false)
        {
            try
            {
                using var archive = new ZipArchive(
                    source,
                    ZipArchiveMode.Read,
                    leaveOpen: leaveOpen);
                var entries = new List<ZipEntry>(archive.Entries.Count);
                long total = 0;
                foreach (var entry in archive.Entries)
                {
                    if (entry.Length > MaximumEntryBytes)
                    {
                        throw new InvalidDataException("Zip entry exceeds safe limit");
                    }
                    total = checked(total + entry.Length);
                    if (total > MaximumArchiveBytes)
                    {
                        throw new InvalidDataException("Zip archive exceeds safe limit");
                    }
                    using var reader = entry.Open();
                    using var content = new MemoryStream((int)entry.Length);
                    reader.CopyTo(content);
                    entries.Add(new ZipEntry(entry.FullName, content.ToArray()));
                }
                return entries;
            }
            finally
            {
                if (!leaveOpen)
                {
                    source.Dispose();
                }
            }
        }

        private void ThrowIfDisposed() =>
            ObjectDisposedException.ThrowIf(_disposed, this);
    }

    public sealed class ZipInputStream : Stream
    {
        private readonly ZipFile _archive;
        private int _index = -1;
        private Stream? _current;

        public ZipInputStream(string fileName)
        {
            _archive = ZipFile.Read(fileName);
        }

        public ZipEntry? GetNextEntry()
        {
            _current?.Dispose();
            _current = null;
            _index++;
            if (_index >= _archive.Count)
            {
                return null;
            }
            var entry = _archive[_index];
            _current = entry.OpenReader();
            return entry;
        }

        public override int Read(byte[] buffer, int offset, int count) =>
            Current.Read(buffer, offset, count);

        public override int Read(Span<byte> buffer) => Current.Read(buffer);

        public override bool CanRead => _current?.CanRead ?? false;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => Current.Length;

        public override long Position
        {
            get => Current.Position;
            set => throw new NotSupportedException();
        }

        public override void Flush()
        {
        }

        public override long Seek(long offset, SeekOrigin origin) =>
            throw new NotSupportedException();

        public override void SetLength(long value) =>
            throw new NotSupportedException();

        public override void Write(byte[] buffer, int offset, int count) =>
            throw new NotSupportedException();

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                _current?.Dispose();
                _archive.Dispose();
            }
            base.Dispose(disposing);
        }

        private Stream Current =>
            _current ?? throw new InvalidOperationException("No active zip entry");
    }
}
