// Practice fixture: classify nonnegative integers. There is a bug at n = 1.
exports.isPrime = n => n === 1 || (n > 1 && !Array.from({length: n - 2}, (_, i) => i + 2).some(d => n % d === 0));
