try:
    try:
        raise KeyError("test")
    except IndexError as e:
        print("Caught:", str(e))
    except (ValueError, IOError) as e:
        print("Caught3:", str(e))
    finally:
        print("Finally1")

    print("Continue")

except KeyError as e:
    print("Caught2:", str(e))
finally:
    print("Finally2")
print("End")
