namespace Unnameko.Desktop;

internal enum AvatarExpression
{
    Neutral,
    Happy,
    Shy,
    Worried,
    Sleepy,
    Focused,
}

internal enum AvatarMouthFrame
{
    Closed,
    Half,
    Open,
}

internal sealed class AvatarExpressionStateMachine
{
    private AvatarExpression _expression = AvatarExpression.Neutral;
    private DateTimeOffset _holdUntil = DateTimeOffset.MinValue;
    private int _mouthSequence;

    public AvatarExpression Expression => _expression;
    public bool IsBlinking { get; private set; }
    public bool IsSpeaking { get; private set; }
    public AvatarMouthFrame MouthFrame { get; private set; }

    public bool Request(AvatarExpression expression, TimeSpan? hold = null, bool force = false)
    {
        var now = DateTimeOffset.UtcNow;
        if (!force && expression != _expression && now < _holdUntil) return false;
        var changed = expression != _expression;
        _expression = expression;
        _holdUntil = now + (hold ?? TimeSpan.FromMilliseconds(900));
        return changed;
    }

    public void SetBlinking(bool blinking) => IsBlinking = blinking;

    public void SetSpeaking(bool speaking)
    {
        IsSpeaking = speaking;
        _mouthSequence = 0;
        MouthFrame = AvatarMouthFrame.Closed;
    }

    public AvatarMouthFrame AdvanceMouth()
    {
        if (!IsSpeaking)
        {
            MouthFrame = AvatarMouthFrame.Closed;
            return MouthFrame;
        }
        // Closed frames between openings make the three-frame cycle read as speech
        // without producing a mechanical constant flap.
        AvatarMouthFrame[] sequence =
        [
            AvatarMouthFrame.Half,
            AvatarMouthFrame.Open,
            AvatarMouthFrame.Half,
            AvatarMouthFrame.Closed,
            AvatarMouthFrame.Half,
            AvatarMouthFrame.Closed,
        ];
        MouthFrame = sequence[_mouthSequence++ % sequence.Length];
        return MouthFrame;
    }

    public string ResolveAssetName()
    {
        if (IsBlinking) return "blink.png";
        if (IsSpeaking)
        {
            return MouthFrame switch
            {
                AvatarMouthFrame.Half => "talk_half.png",
                AvatarMouthFrame.Open => "talk_open.png",
                _ => "happy.png",
            };
        }
        return _expression switch
        {
            AvatarExpression.Happy => "happy.png",
            AvatarExpression.Shy => "shy.png",
            AvatarExpression.Worried => "worried.png",
            AvatarExpression.Sleepy => "sleepy.png",
            AvatarExpression.Focused => "focused.png",
            _ => "neutral.png",
        };
    }

    public static AvatarExpression FromStatus(string mood, double energy, bool working)
    {
        if (working) return AvatarExpression.Focused;
        if (energy < 0.28 || mood.Contains("困") || mood.Contains("疲")) return AvatarExpression.Sleepy;
        if (mood.Contains("不安") || mood.Contains("紧张") || mood.Contains("难过")) return AvatarExpression.Worried;
        if (mood.Contains("开心") || mood.Contains("高兴") || mood.Contains("期待")) return AvatarExpression.Happy;
        if (mood.Contains("害羞")) return AvatarExpression.Shy;
        return AvatarExpression.Neutral;
    }
}
